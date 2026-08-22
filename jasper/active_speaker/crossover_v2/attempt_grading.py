# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a VERIFY capture is a new tuning attempt, and how it grades (5b-ii).

The other meter in the package.  :mod:`.admission`
owns *one position's* attempt ledger — the bounded-retry meter in front of a
capture, counted in extras and spent within a session.  This one owns the
**tuning-attempt** ledger the S3 loop keeps *across* sessions: the history a
recovered session hydrates, the budget
:func:`~jasper.active_speaker.attempts_loop.decide_next` grades against, and the
question a post-apply VERIFY asks of both — *is this capture a new attempt at
all, and may the loop grade it?*

Two meters, two modules, deliberately.  They count different things over
different lifetimes, and a household that has spent its extras at position 3 has
not thereby spent a tuning attempt.  Folding this into :mod:`.admission` would
put two budgets under one name, which is the drift this campaign removes.

**This module DECIDES; it does not act** — the split :mod:`.accountability`
established and :mod:`.admission` and :mod:`.capture_dispatch` restated.  Here
that rule carries more weight than usual, because the act it stays away from is
an **irreversible durable write**: banking prediction error into
:mod:`~jasper.active_speaker.model_error_store`.  So the boundary is drawn at
the write and stated twice — once here and once at the session's call site:

* :func:`assess_attempt_identity` runs **before** the write, and answers the
  only question that can prevent it.
* :func:`grade_attempt_outcome` runs **after** it, and answers only what the
  loop does with a record the write has already been asked about.

Nothing in between is this module's.  The seam call, the guard that decides
whether to make it, its ``except`` arms, all of its log lines, and the identity
conflict it can report all stay on the session, unchanged and in one place, so
"how many times can this write fire" is answered by reading one method rather
than by tracing a callable through a module boundary.  That is the same reason
:mod:`.admission` left the group close under ``_close_lock`` with its caller:
the split goes at the boundary that matters, and here the boundary is the write.

**The two ledgers stay separate, and that is a #2291 requirement rather than a
preference.**  ``model_error_store`` owns prediction/realization error; the
attempts ledger owns the acoustic grade.  The numbers coincide today, which is
exactly the hazard — a future change to what the LEDGER grades must not silently
change what the STORE banks.  Nothing here reads, derives, or reinterprets the
banked quantity: :func:`grade_attempt_outcome` never sees it, and
:func:`assess_attempt_identity` decides an *identity*, not a number.

**No household copy lives here** — the rule :mod:`.coordinator` states.  The two
ladders answer with *kinds*; :mod:`.refusal_copy` owns ``REASON_REGISTRY`` and the
sentences it binds, and the flow still constructs the decision payload the
household reads.  The kernel's own vocabulary (``LoopDecision``,
``AttemptBudget``, ``STOP_EVIDENCE``, ``REASON_ATTEMPT_NOT_COMPARABLE``) stays
with the flow, because a ladder that answers with a kind never touches the
carrier — so this module needs no import of
:mod:`~jasper.active_speaker.attempts_loop` at all, and does not have one.

Two values below are the exception that proves the rule, moved here by #2291
Phase 5c-ii because this is the module that *decides* with them:
:data:`ATTEMPT_REASON_NO_FLOOR` is a grading **status**, not a sentence — it has
no ``REASON_REGISTRY`` entry and no copy — and
:data:`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` is a policy threshold.  Neither
is household-facing text.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "ATTEMPT_ALREADY_RECORDED",
    "ATTEMPT_NEW",
    "ATTEMPT_REASON_NO_FLOOR",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "GRADE_DECIDE_NEXT",
    "GRADE_KINDS",
    "GRADE_NOT_COMPARABLE",
    "GRADE_NO_FLOOR",
    "IDENTITY_KINDS",
    "AttemptIdentity",
    "assess_attempt_identity",
    "grade_attempt_outcome",
]


# --------------------------------------------------------------------------- #
# the two values this module decides with (#2291 Phase 5c-ii)
# --------------------------------------------------------------------------- #

# A grading status, deliberately not a synthetic kernel decision. The kernel
# requires a real FloorStats and the store returns ``None`` until an offline
# repeat study adopts one, so the honest live result is ungraded — no invented
# floor and no improvement claim.
ATTEMPT_REASON_NO_FLOOR = "ungraded_no_floor"

# How much the correction must improve ITS OWN two-branch model before a
# spec-failing prediction is recorded as a material improvement
# (linearization-integrity PR-L4 item 2). Both numbers are the pooled spec
# residual (`flat_spec.spec_convergence_residual`) of the RAW pre-fit and the
# LINEARIZED predicted sum, graded through the identical evaluator, in dB.
#
# **It decides a LEDGER value, not an apply.** Until the nanny burn-down
# (docs/measurement-loop-doctrine.md deviation (c)) falling short of this
# number REFUSED the round at the confirm seam; now it chooses between
# `accountability.LEDGER_IMPROVED` and `LEDGER_NOT_AN_IMPROVEMENT`, and the
# measured round decides what happens next. Every derivation below is
# unchanged by that — the question the number answers is the same one.
#
# It only bites when the prediction ALREADY fails the spec — a prediction
# that meets it needs no improvement argument, and judging an in-spec result on
# "how much did it improve" would read the flattest speakers worst. So the
# question this threshold answers is narrow: *we can already see this will not
# reach spec — is it at least clearly moving the right way?*
#
# 0.5 dB, and the derivation changed with the frame (PR-L4 review B1). While
# this compared the model against the measured in-room cloud, the threshold had
# to absorb the whole cross-frame gap and was set at `SPEC_BANDS[0]`'s 1.5 dB
# for that reason — which, as the review demonstrated, made the verdict a
# function of the ROOM rather than the correction. Now that both terms are the
# same instrument (same branches, same grid, same evaluator, differing ONLY by
# the emitted filters) the comparison carries no measurement noise at all, so
# the threshold is a product-policy floor instead of a noise margin.
#
# 0.5 dB is that floor because it is this model's own measured tracking error:
# `crossover_v2.intervention.plan_linearization` records the complex-correction
# model tracking the real
# VERIFY summation to ~0.5 dB on JTS3 (the zero-phase model it replaced
# mistracked by ~2.0 dB). An improvement smaller than the gap between what we
# model and what the hardware realizes is not an improvement we can honestly
# claim, so it is not recorded as one.
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5


# --------------------------------------------------------------------------- #
# identity — the rung in front of the durable write
# --------------------------------------------------------------------------- #

#: This capture measures a candidate the ledger has never recorded. The caller
#: may build its record and ask the store to bank prediction error for it.
ATTEMPT_NEW = "new_attempt"
#: The applied candidate is already in accepted history. A repeated successful
#: re-verify of it is not a new tuning attempt, and it must not double-write
#: model error — the one rung that stands between a replayed capture and a
#: second durable observation of the same identity.
ATTEMPT_ALREADY_RECORDED = "already_recorded"

#: What :func:`assess_attempt_identity` can answer. Declared as a set so the
#: caller can check its arms for completeness rather than trust them; the
#: dangerous direction there is the PERMISSIVE one, since a kind that falls
#: through to "proceed" reaches the write.
IDENTITY_KINDS = frozenset({ATTEMPT_NEW, ATTEMPT_ALREADY_RECORDED})


@dataclass(frozen=True)
class AttemptIdentity:
    """Which attempt this capture is, and whether the ledger already has it."""

    #: One of :data:`IDENTITY_KINDS`.
    kind: str
    #: The id this capture's record would carry. Decided even when the answer
    #: is :data:`ATTEMPT_ALREADY_RECORDED`, because it is *how* that answer was
    #: reached — a caller that logs the skip names the same id the match was on.
    attempt_id: str


def assess_attempt_identity(
    *,
    tuning_attempt_id: str | None,
    candidate_fingerprint: Callable[[], str | None],
    session_id: str,
    capture_attempt: int,
    recorded_ids: Iterable[str],
) -> AttemptIdentity:
    """Name this capture's attempt, and say whether the ledger already has it.

    The identity is the APPLIED candidate's, most specific first: the tuning
    attempt id the durable state carries, then the built candidate's own
    fingerprint, and only if neither exists a session-scoped fallback that is
    unique per capture. The order is the point — on the stage that grades a
    round the first is the only one populated, and a capture that shares an
    applied candidate must share its id, or the dedup below cannot see a repeat.

    ``candidate_fingerprint`` is a **callable** for the reason
    :func:`~.admission.settle_spent_slot`'s ``is_group`` is one — call count. It
    reaches a host object the session holds, and the shipped flow does not
    read it while a tuning attempt id is in hand; a value resolved at call time
    would reach into that object on every graded capture, which is a read the
    flow does not make. It answers ``None`` when there is no candidate to ask.

    ``recorded_ids`` is an :class:`~typing.Iterable` rather than a collection so
    a caller may pass a generator over its history and keep the shipped
    short-circuit: the scan stops at the first match, exactly as the ``any(...)``
    it replaces did.

    **Why the answer is a kind and the write is not here.** Returning
    :data:`ATTEMPT_ALREADY_RECORDED` is the only thing that prevents a second
    durable observation of one candidate identity, so it is stated as a ruling
    and asked before the record exists — but the write it guards belongs to the
    caller, which owns the seam. See the module docstring for that boundary.
    """
    candidate_id = tuning_attempt_id
    if not candidate_id:
        candidate_id = candidate_fingerprint()
    attempt_id = candidate_id or f"{session_id}:{capture_attempt}"
    for recorded in recorded_ids:
        if recorded == attempt_id:
            return AttemptIdentity(ATTEMPT_ALREADY_RECORDED, attempt_id)
    return AttemptIdentity(ATTEMPT_NEW, attempt_id)


# --------------------------------------------------------------------------- #
# grading — the rung behind it
# --------------------------------------------------------------------------- #

#: The capture failed its own integrity checks, so there is nothing to grade
#: and the loop stops on evidence grounds (#2033).
GRADE_NOT_COMPARABLE = "not_comparable"
#: The capture is usable but this speaker has adopted no claim floor, so the
#: attempt is recorded ungraded rather than graded against nothing.
GRADE_NO_FLOOR = "no_floor"
#: Both preconditions hold: the loop grades this attempt against the floor.
GRADE_DECIDE_NEXT = "decide_next"

#: What :func:`grade_attempt_outcome` can answer. The caller checks membership
#: rather than trusting its arms, and degrades toward
#: :data:`GRADE_NOT_COMPARABLE` — see that function for which direction is safe.
GRADE_KINDS = frozenset({
    GRADE_NOT_COMPARABLE,
    GRADE_NO_FLOOR,
    GRADE_DECIDE_NEXT,
})


def grade_attempt_outcome(*, comparable: bool, floor_present: bool) -> str:
    """May the loop grade this attempt, and if not, which refusal is honest?

    Two rungs, and **the order is the ruling**: evidence refusal outranks
    grading preconditions. A capture that failed its integrity checks is
    answered as a capture problem even on a speaker that has adopted no claim
    floor, because #2033's integrity result is meaningful without one — and
    letting the no-floor status answer first would report "we have no floor to
    grade against" about a capture that was never gradeable in the first place,
    masking a rejected recording behind a configuration sentence.

    The absent floor is second and is not a refusal at all: the attempt is
    recorded, and the loop simply has no claim to make about it yet.

    Both inputs are plain values. Unlike
    :func:`assess_attempt_identity`'s fingerprint they are already in the
    caller's hand when this is asked — the record exists by then, and reading
    ``integrity.comparable`` off it can neither raise nor be observed.

    **The safe direction, for the caller's fallback.** An unrecognised kind must
    degrade to :data:`GRADE_NOT_COMPARABLE`, never to :data:`GRADE_DECIDE_NEXT`:
    grading is the arm that makes a *claim* about the speaker, and one made on
    an answer nobody wired would put an unearned improvement sentence in front
    of a household. Refusing on evidence grounds says the loop could not judge
    this attempt, which is exactly what a wiring defect means.
    """
    if not comparable:
        return GRADE_NOT_COMPARABLE
    if not floor_present:
        return GRADE_NO_FLOOR
    return GRADE_DECIDE_NEXT
