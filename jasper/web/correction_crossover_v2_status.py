# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The web adapter over the v2 status projection.

The derivations live in
:mod:`jasper.active_speaker.crossover_envelope_v2`'s status-projection
section, which may not import this layer. This module supplies the answers
only the web host holds — the loaded state, the volume plan, the review
decision, the republish door's admission — and shapes what comes back into
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
from jasper.log_event import log_event
from jasper.web import correction_crossover_v2 as _host


def _previous_candidate_fingerprint(state: Mapping[str, Any] | None) -> str | None:
    """The measured candidate the applied graph displaced, if one is recorded.

    Reads the ``previous_candidate_fingerprint`` the apply path records
    (``observe_apply_success``, off the displaced profile's own
    ``source.measured_candidate_fingerprint`` — the identity
    ``baseline_profile._source_payload`` writes). A state written before the
    field existed reads as "no previous candidate" — the way back returns at
    the next apply, with no schema bump.

    Three readers, one field: the status block below (the wizard's way-back
    action), the host's ``_previous_candidate_known`` seam (the adoption
    table's ``rollback_available``), and the auto-revert's target resolution
    (``bind_delta_probe_rollback``). Sharing the read is what keeps the
    capability answer and the action aimed at one identity.
    """
    value = (state or {}).get("previous_candidate_fingerprint")
    return value if isinstance(value, str) and value else None


def _offerable_previous_candidate(state: Mapping[str, Any] | None) -> str | None:
    """The way-back fingerprint, published only when its door would admit it.

    :func:`_previous_candidate_fingerprint`'s value, gated on
    :func:`~jasper.web.correction_crossover_v2_republish.republish_preflight`
    — the republish door's own read-only admission — so no screen advertises
    a "Go back to the previous tuning" the door then refuses (a pruned bank,
    a corrupted artifact, a corner the declaration no longer carries a
    revision for). Deliberately NOT gated on the automatic revert's pairing:
    that gates only the AUTO path, and the button is the household's.

    Fails CLOSED on an unexpected preflight error — a button that cannot be
    vouched for is withheld, with the cause in the journal — because the
    alternative is the advertised-then-refused drift this gate exists to
    remove.
    """
    from jasper.web import correction_crossover_v2_republish as republish_door

    fingerprint = _previous_candidate_fingerprint(state)
    if fingerprint is None:
        return None
    try:
        admitted = republish_door.republish_preflight(fingerprint) is None
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            _host.logger,
            "correction.crossover_v2_way_back_preflight_failed",
            level=logging.WARNING,
            exc_info=True,
        )
        return None
    return fingerprint if admitted else None


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = _host.load_v2_state()
    session_id = (state or {}).get("session_id")
    attempts = (state or {}).get("attempts_loop")
    attempts = attempts if isinstance(attempts, Mapping) else {}
    last_attempt_decision = attempts.get("last_decision")
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
        # The session's own clock (#1947), and the ONLY one this flow needs:
        # ``save_v2_state`` stamps it on every write, and every write site is a
        # session TRANSITION — a consumed capture, a review decision, an apply,
        # a terminal failure, a start-over. No poll writes state, so the file's
        # last write IS the session's last real activity, and a second
        # per-record stamp would be a second clock to keep honest. The envelope
        # reads it to tell a session the household is still inside from one that
        # ended (``crossover_envelope_v2._session_is_live``). ``None`` when
        # there is no state file, which reads as "cannot say this is current".
        "updated_at": (state or {}).get("updated_at"),
        # The commission tier behind whatever this block reports, or ``None``
        # when the durable state does not say (pre-tier state, or a session
        # that declared none). Never defaulted to "full" — see
        # ``persist_conductor_state``.
        "tier": (str((state or {}).get("tier") or "") or None),
        # Which sub-moment of the measuring session's tail this is, when
        # ``phase`` is ``closing`` (two-stage D1). ``""`` everywhere else.
        "cloud_close": str((state or {}).get("cloud_close") or ""),
        "candidate": (state or {}).get("candidate"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        # The last graded round's adoption receipt — what it decided, which row
        # decided it, and where it sat in the series (#2537, #2602).
        #
        # **This projection was missing**, and the envelope has read ``None``
        # here on every real box since #2537: ``persist_conductor_state`` wrote
        # ``state["round_receipt"]`` and this block never forwarded it, so the
        # done screen's round key and its keep-for-iteration caveat existed only
        # in unit tests that hand-built the status dict. #2602 makes that gap
        # load-bearing rather than merely wasteful — a series that cannot tell a
        # household another round is coming has not delivered the ruling — so it
        # is fixed here rather than filed. Copied through unvalidated, exactly
        # like ``candidate`` and ``verify`` beside it: the envelope's own
        # accessor is the validating reader.
        "round_receipt": (state or {}).get("round_receipt"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        # The banked-candidate way back: the fingerprint of the measured
        # candidate the applied graph displaced, or ``None`` (a first-ever
        # apply, a prior profile that was not a measured-candidate apply, or
        # a pointer the republish door would refuse). The envelope mints a
        # /crossover/v2/republish action from it; publishing it only when
        # the door's own read-only admission passes is what keeps the button
        # and the door one answer (#2291's non-drift rule). The door still
        # re-runs the admission on POST — the bank can change between the
        # answer and the action.
        "previous_candidate_fingerprint": _offerable_previous_candidate(state),
        "session_id": session_id,
        # Minimal live-loop observability: no attempt curves/history on the
        # household polling path, only the kernel output the envelope formats
        # and the durable model-error record count.
        "attempts_loop": {
            "last_decision": (
                dict(last_attempt_decision)
                if isinstance(last_attempt_decision, Mapping) else None
            ),
            "store_count": store_count,
        },
        "cloud": _projection.compact_cloud_status(
            (state or {}).get("cloud"), current_session_id=session_id,
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
    block["post_apply_grade"] = _host._post_apply_grade(block)
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
