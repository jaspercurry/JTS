# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session grade vocabulary and grading."""

from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.active_speaker import crossover_envelope_v2 as projection
from jasper.active_speaker.crossover_contract import REASON_APPLIED_GRADE_MARK_ONLY
from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
from jasper.active_speaker.crossover_v2.verification import (
    RESULT_INCONCLUSIVE,
    RESULT_KEEP_PREVIOUS,
    RESULT_VERIFIED_BEST_EVALUATED,
    RESULT_VERIFIED_TARGET,
)
from jasper.active_speaker.grade_coverage import asked_beyond_mark
from jasper.json_fields import finite_float


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

#: The post-apply SPATIAL grade's own state (#2160). ``overall_within_target`` is a
#: bool and therefore cannot distinguish "graded and failed" from "could not be
#: graded at all" — the gauge's ``passed``
#: is ``False`` for an unmeasurable spectrum too, by its own "will not report a
#: clean bill of health for a spectrum it could not fully measure" rule. This
#: field carries the distinction the verdict key structurally cannot.
GRADE_SPATIAL_ABSENT = "absent"
GRADE_SPATIAL_PASSED = "passed"
GRADE_SPATIAL_FAILED = "failed"
GRADE_SPATIAL_UNMEASURABLE = "unmeasurable"


def _spatial_grade(post_apply: Any) -> str:
    """One post-apply cloud entry reduced to its SPATIAL grade state.

    ``overall_within_target`` — projected by
    :func:`~jasper.active_speaker.crossover_envelope_v2.compact_cloud_status`
    from the
    spec report — stays THE consumed verdict key: every existing verdict path
    reads it, and ``flatness.passed`` is the same value under another name, so
    this deliberately does not become a second reader of it.
    ``flatness.evaluable`` is consulted for exactly one thing, the distinction
    ``overall_within_target`` cannot carry: a spectrum where no band survived to be
    measured reports ``within_target=False`` and is NOT a failure.

    Unmeasurable is claimed only on POSITIVE evidence (``evaluable`` present
    and ``False``). A durable state whose entry carries no ``flatness`` at all
    — an available pipeline written before the gauge shipped — leaves the only
    verdict that exists standing, because downgrading a recorded failure to
    "could not be measured" on the ABSENCE of a gauge would be the fabricated
    reading this program forbids, pointed the other way.
    """
    if not isinstance(post_apply, Mapping):
        return GRADE_SPATIAL_ABSENT
    within_target = post_apply.get("overall_within_target")
    if not isinstance(within_target, bool):
        # No verdict — the group never closed, or its pipeline never became
        # available. Never a failing grade; see ``_spec_verdict``'s own
        # "absence of a verdict is not a failing one" rule.
        return GRADE_SPATIAL_ABSENT
    if within_target:
        return GRADE_SPATIAL_PASSED
    flatness = post_apply.get("flatness")
    if isinstance(flatness, Mapping) and flatness.get("evaluable") is False:
        return GRADE_SPATIAL_UNMEASURABLE
    return GRADE_SPATIAL_FAILED


def post_apply_grade(
    state: Mapping[str, Any] | None, *, applied_profile: Any, block: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The grade the status block publishes as ``post_apply_grade``, read from
    the durable ``state`` and the applied profile alone. ``block`` is a status
    block that already holds the fields the grade reads."""
    state = state or {}
    applied = bool(state.get("applied"))
    return _post_apply_grade(block if block is not None else {
        "applied": applied, "candidate": state.get("candidate"), "verify": state.get("verify"),
        "prediction": projection.prediction_status(state),
        "cloud": projection.compact_cloud_status(state.get("cloud"), current_session_id=state.get("session_id")),
    }, spatial_required=applied and asked_beyond_mark(state, applied_profile=applied_profile))


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

    The returned ``state`` is one of the ``GRADE_*`` constants above;
    ``graded`` answers only "was it checked" — since R19 it is no longer a
    boolean a caller may key "all clear" on by itself; ``scope``/``spatial``/
    ``complete`` below carry the verdict it cannot. Both a passing VERIFY
    outcome and a graded post-apply cloud count — either instrument is a real
    check. A mark-VERIFY that
    FAILED caps ``state`` whatever the cloud group says (#2464); the
    derivation below owns that rule and states why.

    **``state`` answers "was it checked"; ``scope``/``spatial``/``complete``
    answer "how widely, and was that enough" (R19, #2098 + #2160).** Those
    three are why this returns more than a state name. ``state`` alone cannot
    carry either fact, and both were being guessed at downstream:

    * a run that asked for poses beyond the mark but whose post-apply group
      never closed reaches ``mark_verified`` — a true local result, short of
      what its plan asked. It rendered as "applied and graded".
    * a post-apply group that closed with ``overall_within_target=False`` reaches
      ``GRADE_GRADED``, because a graded-and-failed group IS graded. It also
      rendered as "applied and graded" — measured on jts3 2026-08-07, a
      −4.63 dB spatial miss under a green tick.

    ``scope`` is what the evidence DELIVERED; the persisted run manifest's
    asked poses state what the run PROMISED. ``complete`` compares the two,
    so the wizard, ``/state`` and doctor do not each derive that fact. Records
    without a plan retain delivery-only grading (ADR-0298): an old session
    never made a spatial promise merely because a later build knows one.

    ``spatial_worst_db``/``_hz`` are copied from the same ``flatness`` gauge
    the doctor's cloud-pipeline line prints, never re-derived, so "the grade
    failed" and "by how much" cannot drift apart. ``None`` whenever the gauge
    reports no number, including a failed grade whose gauge is absent.

    **Grades and discloses; never gates** (#2160 ruling). A failed spatial
    grade is a COMPLETED grade: the session completes, the applied tune stays,
    the failure is loud. Nothing here reverts anything — see the
    surface-not-auto-restore paragraph above, which this extends rather than
    revisits.
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
    # correction checked AFTERWARDS. VERIFY and the post-apply group answer that
    # by themselves — that is why dropping the selector consultation is sound
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
            "spatial": GRADE_SPATIAL_ABSENT,
            "spatial_worst_db": None,
            "spatial_worst_hz": None,
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
    prediction = block.get("prediction")
    prediction = prediction if isinstance(prediction, Mapping) else {}
    comparison = prediction.get("comparison")
    comparison = comparison if isinstance(comparison, Mapping) else {}
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
    cloud = block.get("cloud")
    post_apply = cloud.get(PHASE_CLOUD_VERIFY) if isinstance(cloud, Mapping) else None
    cloud_verdict = (
        post_apply.get("overall_within_target") if isinstance(post_apply, Mapping) else None
    )
    # **A failed mark-VERIFY caps this badge whatever the group says** (#2464,
    # ruled 2026-08-19). ``cloud_verdict`` was tested FIRST, so a closed group
    # made the fail and inconclusive arms unreachable: a re-verify that failed
    # against a carried-forward passing group reached ``GRADE_GRADED`` with
    # ``graded=True``, and every surface keying on those read it as all clear.
    verify_failed = outcome == "fail" or CLAIM_FAIL in {
        tracking_status, absolute_status,
    }
    no_claim_graded = bool(claims) and not {tracking_status, absolute_status} & {
        CLAIM_PASS, CLAIM_FAIL,
    }
    if verify_failed:
        state = GRADE_FAILED
    elif outcome == "inconclusive":
        state = GRADE_INCONCLUSIVE
    elif isinstance(cloud_verdict, bool):
        # A walked post-apply position group — the widest claim available, and
        # on a clean pass it is the wider claim, so it still wins the word. It
        # is a graded instrument in its own right, so it outranks the
        # ungraded-mark arm below rather than being capped by it.
        state = GRADE_GRADED
    elif no_claim_graded:
        state = GRADE_INCONCLUSIVE
    elif outcome == "pass":
        # Completeness below compares this measured scope with the asked poses.
        state = GRADE_MARK_VERIFIED
    else:
        state = GRADE_UNVERIFIED
    spatial = _spatial_grade(post_apply)
    # Delivered width, derived from the evidence rather than from ``state``:
    # only a real spatial VERDICT is a spatial claim, so a group that closed
    # and could not grade anything reaches back to whatever the mark proved.
    if spatial in {GRADE_SPATIAL_PASSED, GRADE_SPATIAL_FAILED}:
        scope = GRADE_SCOPE_SPATIAL
    elif outcome == "pass":
        scope = GRADE_SCOPE_MARK
    else:
        scope = GRADE_SCOPE_NONE
    complete = scope == GRADE_SCOPE_SPATIAL if spatial_required else scope != GRADE_SCOPE_NONE
    flatness = post_apply.get("flatness") if isinstance(post_apply, Mapping) else None
    flatness = flatness if isinstance(flatness, Mapping) else {}
    return {
        **({"outcome": result_outcome} if result_evidence else {}),
        "state": state,
        "graded": state in {GRADE_GRADED, GRADE_MARK_VERIFIED},
        "verify_outcome": outcome or None,
        "post_apply_spec_passed": cloud_verdict if isinstance(cloud_verdict, bool) else None,
        "scope": scope,
        "spatial": spatial,
        # Only alongside a real failing grade: a number without a verdict to
        # attach it to is the fabricated reading this module forbids.
        "spatial_worst_db": (
            finite_float(flatness.get("max_db"))
            if spatial == GRADE_SPATIAL_FAILED else None
        ),
        "spatial_worst_hz": (
            finite_float(flatness.get("max_hz"))
            if spatial == GRADE_SPATIAL_FAILED else None
        ),
        "complete": complete,
        **({"reason": REASON_APPLIED_GRADE_MARK_ONLY} if scope == GRADE_SCOPE_MARK and not complete else {}),
        "improvement_db": improvement_db,
        "tracking_passed": True if tracking_status == CLAIM_PASS else False if tracking_status == CLAIM_FAIL else None,
        "absolute_passed": True if absolute_status == CLAIM_PASS else False if absolute_status == CLAIM_FAIL else None,
        "absolute_miss_db": absolute_miss_db,
        "absolute_worst_hz": absolute_worst_hz,
        "candidate_fingerprint": str(candidate.get("fingerprint") or "") or None,
    }
