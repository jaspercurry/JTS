# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session grade vocabulary and grading."""

from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.active_speaker.crossover_contract import REASON_APPLIED_GRADE_MARK_ONLY
from jasper.active_speaker.crossover_v2.verification import (
    RESULT_INCONCLUSIVE,
    RESULT_KEEP_PREVIOUS,
    RESULT_VERIFIED_BEST_EVALUATED,
    RESULT_VERIFIED_TARGET,
)
from jasper.active_speaker.grade_coverage import asked_beyond_mark
from jasper.json_fields import as_mapping, finite_float


# The vocabulary of ``crossover_v2.post_apply_grade.state`` (PR-L4 item 4).
# Readers should `.get` against these rather than exhaustively match: a durable
# state written by a later build can carry a name this one has never seen, and
# an unknown state must degrade to "not graded" rather than to a crash.
GRADE_NOT_APPLIED = "not_applied"
GRADE_GRADED = "graded"
# A local pass is distinct from a spatial grade (#2098).
GRADE_MARK_VERIFIED = "mark_verified"
GRADE_INCONCLUSIVE = "inconclusive"
GRADE_FAILED = "failed"
GRADE_UNVERIFIED = "unverified"


#: Delivered coverage, compared below with the run's asked poses (#2098).
GRADE_SCOPE_NONE = "none"
GRADE_SCOPE_MARK = "mark"
GRADE_SCOPE_SPATIAL = "spatial"

#: The post-apply spatial grade's words (#2160).
GRADE_SPATIAL_ABSENT = "absent"
GRADE_SPATIAL_PASSED = "passed"
GRADE_SPATIAL_FAILED = "failed"
GRADE_SPATIAL_UNMEASURABLE = "unmeasurable"

# No path here emits GRADE_GRADED, GRADE_SCOPE_SPATIAL or a GRADE_SPATIAL_*
# word: no instrument grades beyond the mark. jasper/cli/doctor/correction.py
# and its tests still read them.


def grade_inputs(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """The status fields the post-apply grade reads, projected from the durable ``state``."""
    state = state or {}
    priors = as_mapping(state.get("verify_priors"))
    return {
        "applied": bool(state.get("applied")), "candidate": state.get("candidate"), "verify": state.get("verify"),
        "predicted_comparison": as_mapping(priors.get("predicted_spec")).get("comparison"),
    }


def post_apply_grade(
    state: Mapping[str, Any] | None, *, applied_profile: Any, inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The grade the status block publishes as ``post_apply_grade``. ``inputs``
    are :func:`grade_inputs` of ``state``, built here when the caller has not."""
    inputs = grade_inputs(state) if inputs is None else inputs
    return _post_apply_grade(inputs, spatial_required=bool(inputs["applied"]) and asked_beyond_mark(
        state or {}, applied_profile=applied_profile))


def _post_apply_grade(block: Mapping[str, Any], *, spatial_required: bool = False) -> dict[str, Any]:
    """Was the correction now ON the speaker ever checked after it landed?

    **Applied implies graded** (linearization-integrity PR-L4 item 4). A
    session can end ``applied: true`` with no passing post-apply grade — VERIFY
    inconclusive, VERIFY failed and never retried, or a session that simply
    stopped after the apply — and before this the only trace was a phase name
    and an empty ``verify`` block that every surface read as "nothing to
    report". That is how a 10 dB-dark profile sat on JTS3 with a green tick
    over it.

    **Surface, not auto-restore.** The work order allowed either; this is the
    deliberate choice and the reason is that the two failure modes are not
    distinguishable at this seam. A missing grade means "we do not know", and
    the commonest way to reach it is a household that closed the phone after
    the apply — auto-restoring would silently undo a correction that is very
    probably fine, on evidence that says nothing about the correction at all.
    The way back already exists on the done screen,
    and it is the household's call. What was missing is being told.

    The returned ``state`` is one of the ``GRADE_*`` constants above and
    answers "was it checked": VERIFY at the mark is the instrument, and a
    VERIFY that failed caps ``state``. ``scope``/``complete`` answer "how
    widely, and was that enough" (#2098): a run that asked for poses
    beyond the mark still reaches ``mark_verified`` — a true local result,
    short of what its plan asked — so ``graded`` alone is not an all clear.

    ``scope`` is what the evidence DELIVERED; the persisted run manifest's
    asked poses state what the run PROMISED. ``complete`` compares the two,
    so the wizard, ``/state`` and doctor do not each derive that fact. Records
    without a plan retain delivery-only grading (ADR-0298): an old session
    never made a spatial promise merely because a later build knows one.

    **Grades and discloses; never gates** (#2160 ruling). Nothing here reverts
    anything — see the surface-not-auto-restore paragraph above.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_VERIFY_CROSSOVER_REGION,
    )
    from jasper.active_speaker.crossover_v2.contracts import (  # lazy: avoid measurement-stack import cost on unused paths
        CLAIM_FAIL,
        CLAIM_PASS,
    )
    from jasper.active_speaker.crossover_v2_flow import (  # lazy: avoid measurement-stack import cost on unused paths
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    )

    # **This grade reads no ``fc_selection``, on any round.** It once gated its
    # success verdicts on a corner selector's verdict and completeness — "the Fc
    # comparison finished, and the corner on the speaker is the one it
    # authorized". That selector is retired (historical
    # ticket 2.4) along with the corner hunt that fed it, so no round publishes
    # one and this build cannot restate the adjudication of a round that did.
    #
    # This function's own question is the one in its title: was the applied
    # correction checked AFTERWARDS. VERIFY answers that by itself — that is
    # why dropping the selector consultation is sound
    # rather than merely convenient, and it is the same reasoning that already
    # exempted the absent case when the selector was merely unfed.
    #
    # **Read-back tolerance is by non-consumption.** A round banked while a
    # selector existed still carries the payload in durable state; no product
    # read path parses it — not this grade, the status block, the household
    # envelope or the evidence packet — so no legacy shape, well-formed, partial
    # or malformed, can refuse or raise. (Offline archaeology tooling still
    # reads it on purpose: ``scripts/derive-crossover-incident-fixture.py`` mints
    # the #2291 fixture from it, and ``scripts/bank-crossover-round.sh``
    # snapshots it when a bank carries one.)
    # Such a round grades on its OWN verification evidence,
    # which is measured fact about the applied tune rather than a retired
    # comparator's opinion of an alternative. Pinned in
    # ``tests/test_correction_crossover_v2_endpoints.py``.
    if not block.get("applied"):
        # **No cause left, so no claim.** Two instruments could once say that an
        # un-applied round had DELIBERATELY kept the previous tune: a
        # not-an-improvement refusal, which stopped refusing when
        # ``accountability``'s item 2 became a grade (#2854), and the corner
        # selector's ``recommend_alternative``, retired here. Neither exists,
        # so this arm publishes no ``outcome`` at all rather than inventing one
        # — nothing was applied, and nothing measured why.
        return {
            "state": GRADE_NOT_APPLIED,
            "graded": True,
            "verify_outcome": None,
            "scope": GRADE_SCOPE_NONE,
            # Nothing was promised, so nothing is outstanding. `False` here
            # would warn every speaker that has never been commissioned.
            "complete": True,
        }
    candidate = block.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    verify = block.get("verify")
    outcome = str((verify or {}).get("outcome") or "") if isinstance(verify, Mapping) else ""
    claims = verify.get("claims") if isinstance(verify, Mapping) else None
    claims = claims if isinstance(claims, Mapping) else {}
    integration = claims.get("integration")
    integration = integration if isinstance(integration, Mapping) else {}
    absolute = claims.get("absolute")
    absolute = absolute if isinstance(absolute, Mapping) else {}
    tracking_status = str(integration.get("status") or "")
    absolute_status = str(absolute.get("status") or "")
    comparison = as_mapping(block.get("predicted_comparison"))
    improvement_db = finite_float(comparison.get("improvement_db"))
    required_db = finite_float(comparison.get("required_db"))
    absolute_miss_db, absolute_worst_hz = finite_float(absolute.get("max_db")), finite_float(absolute.get("worst_hz"))
    result_evidence = bool(comparison or integration or absolute)
    # The published candidate IS the corner the round executed, so there is no
    # alternative for a winner to have beaten. The fingerprint stays required —
    # it is the evidence that a candidate was published at all.
    authorized_winner = bool(str(candidate.get("fingerprint") or ""))
    material_improvement = (
        str(comparison.get("reason") or "") == "improved"
        and improvement_db is not None
        and required_db is not None
        and math.isclose(
            required_db, PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB, abs_tol=1e-9,
        )
        and improvement_db >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
    )
    verify_regressed = (
        outcome == "fail"
        and str(verify.get("code") or "") != REASON_VERIFY_CROSSOVER_REGION
        if isinstance(verify, Mapping) else False
    )
    # The retired accountability ledger's "the forecast said worse" value; it
    # GRADES here, it does not gate.
    no_material_improvement = (
        str(comparison.get("reason") or "") == "not_an_improvement"
        or improvement_db is not None
        and improvement_db < PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
    )
    if tracking_status == CLAIM_FAIL or verify_regressed or no_material_improvement:
        result_outcome = RESULT_KEEP_PREVIOUS
    elif (
        outcome == "inconclusive"
        or tracking_status not in {CLAIM_PASS, CLAIM_FAIL}
    ):
        result_outcome = RESULT_INCONCLUSIVE
    elif (
        not authorized_winner or outcome != "pass"
        or absolute_status not in {CLAIM_PASS, CLAIM_FAIL}
    ):
        result_outcome = RESULT_INCONCLUSIVE
    elif absolute_status == CLAIM_PASS:
        result_outcome = RESULT_VERIFIED_TARGET
    elif material_improvement and None not in (absolute_miss_db, absolute_worst_hz):
        result_outcome = RESULT_VERIFIED_BEST_EVALUATED
    else:
        result_outcome = RESULT_INCONCLUSIVE
    verify_failed = outcome == "fail" or CLAIM_FAIL in {
        tracking_status, absolute_status,
    }
    no_claim_graded = bool(claims) and not {tracking_status, absolute_status} & {
        CLAIM_PASS, CLAIM_FAIL,
    }
    if verify_failed:
        state = GRADE_FAILED
    elif outcome == "inconclusive" or no_claim_graded:
        state = GRADE_INCONCLUSIVE
    elif outcome == "pass":
        # Completeness below compares this measured scope with the asked poses.
        state = GRADE_MARK_VERIFIED
    else:
        state = GRADE_UNVERIFIED
    scope = GRADE_SCOPE_MARK if outcome == "pass" else GRADE_SCOPE_NONE
    complete = scope != GRADE_SCOPE_NONE and not spatial_required
    return {
        **({"outcome": result_outcome} if result_evidence else {}),
        "state": state,
        "graded": state == GRADE_MARK_VERIFIED,
        "verify_outcome": outcome or None,
        "scope": scope,
        "complete": complete,
        **({"reason": REASON_APPLIED_GRADE_MARK_ONLY} if scope == GRADE_SCOPE_MARK and not complete else {}),
        "improvement_db": improvement_db,
        "tracking_passed": True if tracking_status == CLAIM_PASS else False if tracking_status == CLAIM_FAIL else None,
        "absolute_passed": True if absolute_status == CLAIM_PASS else False if absolute_status == CLAIM_FAIL else None,
        "absolute_miss_db": absolute_miss_db,
        "absolute_worst_hz": absolute_worst_hz,
        "candidate_fingerprint": str(candidate.get("fingerprint") or "") or None,
    }
