# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The S3 tuning loop's improve/stop policy — a pure decision kernel.

:func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` produces the
scalar the flat-linearization plan's S3 loop converges on and says, in its own
docstring, that "S3's loop policy — how much improvement counts, how many
iterations, when to stop — is not here and must not be." **This module is that
policy.** It is the consumer that instrument was landed ahead of.

Nothing here reads a file, opens a socket, plays audio, or imports numpy. It
takes attempts that already carry their grades and returns one
:class:`LoopDecision`. Persistence lives next door in
:mod:`jasper.active_speaker.model_error_store`; the split is deliberate, so
"the kernel performs no I/O" is checkable by reading its imports rather than
by trusting a comment.

Three rules constrain the kernel, all three measured on jts3 with the mic
bolted in place on 2026-07-31 and written up in
``captures/repeat-floor-20260731/README.md``:

1. **Consecutive attempts only.** Graded against a fixed early baseline the
   repeat floor *walks* — +0.0046 dB/repeat (r = +0.81), which accumulates to
   roughly the whole floor within ~15 attempts as the box drifts (level falls
   −0.0032 dB/repeat; thermal is the obvious candidate). Graded against the
   immediate predecessor it is flat (−0.0021 dB/repeat, r = −0.50). So there is
   **no fixed-baseline mode here, and adding one would be a regression**, not a
   feature. :func:`decide_next` reads exactly the last two attempts.

2. **Repeat averaging stops paying after 4** — :data:`MAX_USEFUL_REPEAT_AVERAGES`.

3. **A change smaller than the instrument's own floor is not a change** —
   :attr:`FloorStats.claim_floor_db`.

The kernel grades a *magnitude* against that floor and, separately, asks
whether the change was an improvement. Both questions have to be answerable
before the loop may claim anything, and when either cannot be answered the
decision is :data:`STOP_EVIDENCE` — never a pass. That mirrors
:mod:`jasper.active_speaker.delta_probe`'s ``unavailable`` doctrine and
:class:`~jasper.active_speaker.flat_spec.BandResult`'s "unevaluable is a
first-class outcome, not a fabricated verdict".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Keep going: this attempt is gradeable and the loop has somewhere to go.
CONTINUE = "continue"
#: Nothing material is left to win, or the tune is already in spec.
STOP_CONVERGED = "stop_converged"
#: The change from the predecessor is smaller than what the instrument can
#: resolve. Continuing would be chasing the instrument's own noise.
STOP_FLOOR = "stop_floor"
#: The attempt budget is spent.
STOP_BUDGET = "stop_budget"
#: The evidence does not support grading this attempt at all. Never a pass.
STOP_EVIDENCE = "stop_evidence"

DECISIONS: frozenset[str] = frozenset({
    CONTINUE, STOP_CONVERGED, STOP_FLOOR, STOP_BUDGET, STOP_EVIDENCE,
})

#: The grade was predicted by a fit, never measured after applying it.
PROVENANCE_MODEL_GRADED = "model-graded"
#: The grade was measured from a capture taken after the tune was applied.
PROVENANCE_REALIZED = "realized"

PROVENANCES: frozenset[str] = frozenset({
    PROVENANCE_MODEL_GRADED, PROVENANCE_REALIZED,
})

#: A measured repeat study: :attr:`FloorStats.claim_floor_db` is derived from
#: an observed p95 by :data:`CLAIM_FLOOR_P95_MULTIPLE`.
FLOOR_BASIS_MEASURED = "measured_repeat_study"
#: A declared policy bar. No repeat study measured this metric; a shipped
#: threshold stands in for one, and says so in every output.
FLOOR_BASIS_POLICY = "declared_policy_bar"

FLOOR_BASES: frozenset[str] = frozenset({
    FLOOR_BASIS_MEASURED, FLOOR_BASIS_POLICY,
})

# Reasons. Free-form strings would drift between the kernel, the replay driver
# and the report; naming them here keeps one spelling per fact.
REASON_AWAITING_FIRST_ATTEMPT = "awaiting_first_attempt"
REASON_BASELINE_ESTABLISHED = "baseline_established"
REASON_ATTEMPT_NOT_COMPARABLE = "attempt_not_comparable"
REASON_PREDECESSOR_NOT_COMPARABLE = "predecessor_not_comparable"
REASON_FLOOR_METRIC_MISMATCH = "floor_metric_mismatch"
REASON_PROVENANCE_MISMATCH = "provenance_mismatch"
REASON_NO_DEVIATION_AVAILABLE = "no_deviation_available"
REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR = "direction_unknown_above_floor"
REASON_GRADED_BINS_SHRANK = "graded_bins_shrank"
REASON_BELOW_CLAIM_FLOOR = "below_claim_floor"
REASON_IMPROVEMENT_ABOVE_FLOOR = "improvement_above_floor"
REASON_REGRESSION_FROM_PREDECESSOR = "regression_from_predecessor"
REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED = "no_material_improvement_predicted"
REASON_IN_SPEC = "in_spec"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

# --------------------------------------------------------------------------
# Constants, with their provenance
# --------------------------------------------------------------------------

#: The honest per-attempt claim floor is **twice** the observed p95 of
#: consecutive-pair deviations. Two-sided: a p95 bounds how big a *no-change*
#: deviation gets, and both attempts in a pair carry that error independently,
#: so a change has to clear it on both sides before it is the change and not
#: the pair.
#:
#: ``captures/repeat-floor-20260731/README.md`` states this rule as "≥ 0.2 dB …
#: 2× the consecutive-pair p95 (0.085 dB)". **That 0.2 is the same rule at
#: conservative display rounding**, not a different threshold: the p95 on the
#: 13 accepted consecutive pairs is 0.08508 dB, so the rule yields 0.17016 dB
#: and the README rounds it up for a household-facing sentence. The kernel
#: computes it; nothing here hardcodes either decimal.
CLAIM_FLOOR_P95_MULTIPLE = 2.0

#: Averaging more than four repeats buys resolution the box's drift immediately
#: spends. Measured 2026-07-31: σ falls as 1/√M, but drift accumulates linearly
#: across the ~21 s each repeat costs, and at M = 4 the two cross —
#: σ/√M ≈ 0.014 dB against ≈ 0.018 dB of accumulated drift.
#:
#: **Numerically equal to two shipped constants it does not mean the same thing
#: as, and must not be unified with** (the house rule from
#: :data:`~jasper.active_speaker.repeat_admission.MAX_RESERVATIONS`'s comment:
#: equal by coincidence of value is not equal by shared meaning):
#: :data:`~jasper.active_speaker.repeat_admission.MAX_ATTEMPTS` is an audible
#: *spend* budget per capture set, refundable on transport failures, and
#: :data:`~jasper.active_speaker.commissioning_capture.DEFAULT_REPEAT_TARGET`
#: is 3 repeats for *outlier rejection*, explicitly "never noise-floor
#: reduction". This one is the point past which noise-floor reduction by
#: averaging stops working. Three different facts.
MAX_USEFUL_REPEAT_AVERAGES = 4


def useful_repeats(requested: int) -> int:
    """Clamp a requested repeat count to what averaging can still pay for.

    The seam the live flow calls when it decides how many repeats to take.
    See :data:`MAX_USEFUL_REPEAT_AVERAGES` for the measurement behind the cap.
    """

    return max(1, min(int(requested), MAX_USEFUL_REPEAT_AVERAGES))


@dataclass(frozen=True)
class AttemptBudget:
    """How many *tuning attempts* one speaker gets.

    Attempts, not measurement repeats — a tune-and-grade cycle, not a
    re-capture. ``target_attempts`` is WO-7's planning number ("about three
    tries") and is **disclosed, not enforced**: it rides on every decision so a
    household surface can render "2 of 3" without a second copy of the number,
    and the only bound the kernel applies is ``hard_cap_attempts``. Two knobs
    enforcing the same thing would be one knob and one bug.

    ``hard_cap_attempts`` is a **policy** bound — target plus one retry, the
    same shape the shipped capture path uses for repeats
    (:data:`~jasper.active_speaker.commissioning_capture.DEFAULT_REPEAT_TARGET`
    3, :data:`~jasper.active_speaker.repeat_admission.MAX_ATTEMPTS` 4). No
    measurement sets it, and this docstring is the place that says so: the
    2026-07-31 repeat study bounded *repeat averaging* at four
    (:data:`MAX_USEFUL_REPEAT_AVERAGES`), which is a different quantity.
    """

    target_attempts: int = 3
    hard_cap_attempts: int = 4

    def __post_init__(self) -> None:
        if self.target_attempts < 1:
            raise ValueError("target_attempts must be at least 1")
        if self.hard_cap_attempts < self.target_attempts:
            raise ValueError("hard_cap_attempts must be >= target_attempts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_attempts": self.target_attempts,
            "hard_cap_attempts": self.hard_cap_attempts,
        }


@dataclass(frozen=True)
class FloorStats:
    """What a change in ``metric`` has to clear before the loop may claim it.

    Build with :meth:`from_repeat_study` when a repeat study measured this
    metric, or :meth:`from_policy_bar` when none has. Both produce a number;
    only the first produces a *measured* one, and :attr:`basis` rides through
    to every decision and every report line so a reader can never mistake one
    for the other.

    Args:
      metric: the grade metric this floor was measured on. :func:`decide_next`
        refuses to grade an attempt whose metric differs — a floor measured on
        one instrument says nothing about another.
      median_db: median consecutive-pair deviation; context for the reader,
        never a threshold. ``None`` on a policy bar.
      p95_db: the p95 the claim floor is derived from. ``None`` on a policy bar.
      claim_floor_db: the threshold itself.
      basis: :data:`FLOOR_BASIS_MEASURED` or :data:`FLOOR_BASIS_POLICY`.
      source: free-text provenance — which study, which shipped constant.
      measured_at: when, as free text (an ISO date for a study, ``""`` when the
        basis is a policy bar and there is no measurement to date).
    """

    metric: str
    claim_floor_db: float
    basis: str
    source: str
    median_db: float | None = None
    p95_db: float | None = None
    measured_at: str = ""

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("FloorStats.metric must be a non-empty name")
        if self.basis not in FLOOR_BASES:
            raise ValueError(f"unknown floor basis {self.basis!r}")
        if not (self.claim_floor_db > 0.0):
            raise ValueError("claim_floor_db must be positive")
        if not self.source:
            raise ValueError("FloorStats.source must say where this came from")

    @classmethod
    def from_repeat_study(
        cls,
        *,
        metric: str,
        median_db: float,
        p95_db: float,
        source: str,
        measured_at: str,
    ) -> "FloorStats":
        """A floor measured by repeating one unchanged measurement.

        ``claim_floor_db`` is computed as
        ``CLAIM_FLOOR_P95_MULTIPLE * p95_db`` — see that constant for why the
        multiple is 2 and why no decimal is transcribed here.
        """

        if not (p95_db > 0.0):
            raise ValueError("p95_db must be positive")
        return cls(
            metric=metric,
            claim_floor_db=CLAIM_FLOOR_P95_MULTIPLE * float(p95_db),
            basis=FLOOR_BASIS_MEASURED,
            source=source,
            median_db=float(median_db),
            p95_db=float(p95_db),
            measured_at=measured_at,
        )

    @classmethod
    def from_policy_bar(
        cls, *, metric: str, claim_floor_db: float, source: str,
    ) -> "FloorStats":
        """A shipped threshold standing in where no repeat study exists.

        Same arithmetic downstream, different epistemics: :attr:`median_db` and
        :attr:`p95_db` stay ``None`` rather than being back-solved from the
        bar, because inventing a p95 to fit a threshold would turn a policy
        choice into a fake measurement.
        """

        return cls(
            metric=metric,
            claim_floor_db=float(claim_floor_db),
            basis=FLOOR_BASIS_POLICY,
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "claim_floor_db": self.claim_floor_db,
            "basis": self.basis,
            "source": self.source,
            "median_db": self.median_db,
            "p95_db": self.p95_db,
            "measured_at": self.measured_at,
        }


@dataclass(frozen=True)
class AttemptIntegrity:
    """Whether this attempt's grade may be compared to another one at all.

    ``comparable`` is the shipped acceptance gate's answer, not a quality
    score: a capture the instrument rejected has no grade, rather than a bad
    one. ``reasons`` carries the gate's own reason strings through unchanged so
    the report can name what actually failed.
    """

    comparable: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"comparable": self.comparable, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class AttemptRecord:
    """One tuning attempt, already graded.

    Grades are **lower-is-better** throughout, which both shipped candidates
    are (``max_db_notch_excluded`` is a deviation; a linearization residual is
    an error). The kernel does not carry a direction flag, because supporting
    a higher-is-better metric that does not exist would be one more branch
    nothing ever takes.

    Two grade shapes are accepted, because the two shipped instruments produce
    two shapes:

    * ``grade_db`` — an absolute grade. Consecutive grades give both the
      magnitude of the change and its direction.
    * ``deviation_from_predecessor_db`` — an unsigned magnitude straight from
      a comparator that only ever produces one (``max_db_notch_excluded``
      re-grades one capture against another and returns how far apart they
      are; there is no absolute per-capture number to subtract). Direction is
      unknown from this alone, and the kernel refuses to claim one.

    When both are present the explicit deviation wins for the magnitude — it is
    the comparator's own answer — and the grades still supply direction.

    Args:
      attempt_id: stable identifier; appears in every decision's basis.
      metric: the grade metric's name. Must match the floor's.
      provenance: :data:`PROVENANCE_MODEL_GRADED` or
        :data:`PROVENANCE_REALIZED`. Comparing across the two is refused —
        a predicted grade and a measured one are different instruments, and
        the household wire requires deltas labelled model-vs-model.
      integrity: the shipped gate's comparability verdict.
      repeats_used: how many repeats this attempt averaged. Disclosed against
        :data:`MAX_USEFUL_REPEAT_AVERAGES`.
      grade_db: absolute grade, lower is better.
      deviation_from_predecessor_db: unsigned magnitude of change from the
        immediately preceding attempt.
      n_graded_bins: how many bins the grade was computed over, when the
        instrument reports it.
        :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual`
        warns that a loop comparing two iterations must be able to tell an
        improvement from a smaller denominator, and says enforcing that is the
        loop's job. :func:`decide_next` enforces it — asymmetrically, since a
        *grown* denominator only makes an improvement harder to win.
      predicted_remaining_improvement_db: what this attempt's fit says is still
        available to win. **Not** ``CrossoverCandidate.flatness_improvement_db``
        — that one is ``anchor_ripple − selected_ripple``, evidence for a delay
        snap already taken, and plugging it in here would read a backward-
        looking number as a forward-looking one.
      in_spec: the fit is already inside spec; nothing left to chase.
      curve_refs: opaque pointers to per-band curves for the report. The kernel
        never dereferences them — it performs no I/O.
    """

    attempt_id: str
    metric: str
    provenance: str
    integrity: AttemptIntegrity
    repeats_used: int = 1
    grade_db: float | None = None
    deviation_from_predecessor_db: float | None = None
    n_graded_bins: int | None = None
    predicted_remaining_improvement_db: float | None = None
    in_spec: bool | None = None
    curve_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("AttemptRecord.attempt_id must be non-empty")
        if not self.metric:
            raise ValueError("AttemptRecord.metric must be non-empty")
        if self.provenance not in PROVENANCES:
            raise ValueError(f"unknown provenance {self.provenance!r}")
        if self.repeats_used < 1:
            raise ValueError("repeats_used must be at least 1")
        if (
            self.deviation_from_predecessor_db is not None
            and self.deviation_from_predecessor_db < 0.0
        ):
            raise ValueError(
                "deviation_from_predecessor_db is a magnitude and cannot be "
                "negative",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "metric": self.metric,
            "provenance": self.provenance,
            "integrity": self.integrity.to_dict(),
            "repeats_used": self.repeats_used,
            "grade_db": self.grade_db,
            "deviation_from_predecessor_db": self.deviation_from_predecessor_db,
            "n_graded_bins": self.n_graded_bins,
            "predicted_remaining_improvement_db": (
                self.predicted_remaining_improvement_db
            ),
            "in_spec": self.in_spec,
            "curve_refs": list(self.curve_refs),
        }


@dataclass(frozen=True)
class LoopDecision:
    """What the loop decided, and every number it decided from.

    ``improved`` is carried separately from ``decision`` on purpose: an
    above-floor *regression* is still :data:`CONTINUE` — the loop keeps working
    rather than accepting a worse tune — and reading that as approval would be
    wrong. ``improved=False`` plus
    :data:`REASON_REGRESSION_FROM_PREDECESSOR` says it in the record. What to
    do about a regression (revert, re-fit, re-measure) is the live flow's
    policy, not this kernel's.
    """

    decision: str
    reason: str
    attempts_used: int
    budget: AttemptBudget
    improved: bool | None = None
    magnitude_db: float | None = None
    improvement_db: float | None = None
    floor: FloorStats | None = None
    basis_attempt_ids: tuple[str, ...] = ()
    provenance: str | None = None
    repeats_over_cap: bool = False
    notes: tuple[str, ...] = ()

    @property
    def should_continue(self) -> bool:
        return self.decision == CONTINUE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "attempts_used": self.attempts_used,
            "budget": self.budget.to_dict(),
            "improved": self.improved,
            "magnitude_db": self.magnitude_db,
            "improvement_db": self.improvement_db,
            "floor": self.floor.to_dict() if self.floor is not None else None,
            "basis_attempt_ids": list(self.basis_attempt_ids),
            "provenance": self.provenance,
            "repeats_over_cap": self.repeats_over_cap,
            "notes": list(self.notes),
        }


def _magnitude_and_improvement(
    previous: AttemptRecord, latest: AttemptRecord,
) -> tuple[float | None, float | None]:
    """The size of the change, and how much of it was an improvement.

    Improvement is ``previous.grade_db - latest.grade_db`` (positive = the
    grade fell = better) and is ``None`` when either grade is absent. The
    magnitude prefers the comparator's own deviation when the instrument
    supplied one, because that number was measured rather than subtracted.
    """

    improvement_db: float | None = None
    if previous.grade_db is not None and latest.grade_db is not None:
        improvement_db = float(previous.grade_db) - float(latest.grade_db)
    if latest.deviation_from_predecessor_db is not None:
        return float(latest.deviation_from_predecessor_db), improvement_db
    if improvement_db is None:
        return None, None
    return abs(improvement_db), improvement_db


def decide_next(
    history: Sequence[AttemptRecord],
    floor: FloorStats,
    *,
    budget: AttemptBudget | None = None,
) -> LoopDecision:
    """Improve or stop, given the attempts so far.

    ``history`` is oldest-first and **only its last two entries are read** —
    rule 1 above. There is deliberately no ``baseline=`` parameter and no way
    to reach further back: on this hardware a fixed early baseline accumulates
    drift comparable to the entire measurement floor within ~15 attempts, so a
    fixed-baseline mode would not be an option, it would be a slow lie.

    When the immediate predecessor is not comparable the kernel refuses to
    grade rather than reaching past it to the last good attempt. Reaching back
    re-opens exactly the drift question rule 1 closed, and there is no banked
    gap-of-two deviation to calibrate what that costs.

    Order of judgement, and why: evidence first (a number that cannot be
    trusted must never reach a threshold), then the floor (a change too small
    to resolve is the most useful thing to say, so it outranks the budget),
    then — only for a decision that would otherwise be :data:`CONTINUE` —
    convergence, then budget. Convergence outranks budget because "there was
    nothing left to win" and "we ran out of tries" are different news and the
    first one is the truth when both hold.
    """

    budget = budget or AttemptBudget()
    attempts_used = len(history)

    if not history:
        return LoopDecision(
            decision=CONTINUE,
            reason=REASON_AWAITING_FIRST_ATTEMPT,
            attempts_used=0,
            budget=budget,
            floor=floor,
        )

    latest = history[-1]
    over_cap = latest.repeats_used > MAX_USEFUL_REPEAT_AVERAGES

    def _decision(
        decision: str,
        reason: str,
        *,
        improved: bool | None = None,
        magnitude_db: float | None = None,
        improvement_db: float | None = None,
        basis_attempt_ids: tuple[str, ...] = (),
        provenance: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> LoopDecision:
        """Every exit carries the run-wide context; only the verdict varies."""

        return LoopDecision(
            decision=decision,
            reason=reason,
            attempts_used=attempts_used,
            budget=budget,
            floor=floor,
            repeats_over_cap=over_cap,
            improved=improved,
            magnitude_db=magnitude_db,
            improvement_db=improvement_db,
            basis_attempt_ids=basis_attempt_ids,
            provenance=provenance,
            notes=notes,
        )

    if not latest.integrity.comparable:
        return _decision(
            STOP_EVIDENCE,
            REASON_ATTEMPT_NOT_COMPARABLE,
            basis_attempt_ids=(latest.attempt_id,),
            provenance=latest.provenance,
            notes=latest.integrity.reasons,
        )

    if latest.metric != floor.metric:
        return _decision(
            STOP_EVIDENCE,
            REASON_FLOOR_METRIC_MISMATCH,
            basis_attempt_ids=(latest.attempt_id,),
            provenance=latest.provenance,
            notes=(f"attempt metric {latest.metric}", f"floor metric {floor.metric}"),
        )

    if attempts_used == 1:
        return _apply_stop_conditions(
            _decision(
                CONTINUE,
                REASON_BASELINE_ESTABLISHED,
                basis_attempt_ids=(latest.attempt_id,),
                provenance=latest.provenance,
            ),
            latest=latest,
        )

    previous = history[-2]
    pair = (previous.attempt_id, latest.attempt_id)

    if not previous.integrity.comparable:
        return _decision(
            STOP_EVIDENCE,
            REASON_PREDECESSOR_NOT_COMPARABLE,
            basis_attempt_ids=pair,
            provenance=latest.provenance,
            notes=previous.integrity.reasons,
        )

    if previous.provenance != latest.provenance:
        return _decision(
            STOP_EVIDENCE,
            REASON_PROVENANCE_MISMATCH,
            basis_attempt_ids=pair,
            notes=(previous.provenance, latest.provenance),
        )

    magnitude_db, improvement_db = _magnitude_and_improvement(previous, latest)
    if magnitude_db is None:
        return _decision(
            STOP_EVIDENCE,
            REASON_NO_DEVIATION_AVAILABLE,
            basis_attempt_ids=pair,
            provenance=latest.provenance,
        )

    def _graded(
        decision: str,
        reason: str,
        *,
        improved: bool | None = None,
        notes: tuple[str, ...] = (),
    ) -> LoopDecision:
        return _decision(
            decision,
            reason,
            improved=improved,
            magnitude_db=magnitude_db,
            improvement_db=improvement_db,
            basis_attempt_ids=pair,
            provenance=latest.provenance,
            notes=notes,
        )

    if magnitude_db < floor.claim_floor_db:
        # `improved` stays None, not False: a sub-floor change is not a
        # regression, it is a change the instrument cannot resolve either way.
        # `improvement_db` still rides along when it exists, disclosed and
        # unclaimable.
        return _graded(STOP_FLOOR, REASON_BELOW_CLAIM_FLOOR)

    if improvement_db is None:
        # Above the floor, but the comparator only reported a magnitude. The
        # tune moved; whether it moved the right way is unknown, and an
        # unknown direction may not be reported as an improvement.
        return _graded(STOP_EVIDENCE, REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR)

    improved = improvement_db > 0.0
    if improved and _denominator_shrank(previous, latest):
        # A grade pooled over fewer bins falls for free. flat_spec hands this
        # hazard to the loop by name; refusing the claim is where it lands.
        return _graded(
            STOP_EVIDENCE,
            REASON_GRADED_BINS_SHRANK,
            notes=(
                f"previous n_graded_bins={previous.n_graded_bins}",
                f"latest n_graded_bins={latest.n_graded_bins}",
            ),
        )

    return _apply_stop_conditions(
        _graded(
            CONTINUE,
            (
                REASON_IMPROVEMENT_ABOVE_FLOOR
                if improved
                else REASON_REGRESSION_FROM_PREDECESSOR
            ),
            improved=improved,
        ),
        latest=latest,
    )


def _denominator_shrank(previous: AttemptRecord, latest: AttemptRecord) -> bool:
    if previous.n_graded_bins is None or latest.n_graded_bins is None:
        return False
    return latest.n_graded_bins < previous.n_graded_bins


def _apply_stop_conditions(
    decision: LoopDecision, *, latest: AttemptRecord,
) -> LoopDecision:
    """Turn a would-be :data:`CONTINUE` into a stop when there is nowhere to go.

    Only reachable from a decision that already survived the evidence and floor
    checks, so a stop here never masks one of those.
    """

    if latest.in_spec:
        return replace(decision, decision=STOP_CONVERGED, reason=REASON_IN_SPEC)
    remaining = latest.predicted_remaining_improvement_db
    if remaining is not None and remaining < material_improvement_db():
        return replace(
            decision,
            decision=STOP_CONVERGED,
            reason=REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED,
            notes=decision.notes + (
                f"predicted remaining {remaining:.4f} dB < "
                f"{material_improvement_db()} dB",
            ),
        )
    if decision.attempts_used >= decision.budget.hard_cap_attempts:
        return replace(
            decision, decision=STOP_BUDGET, reason=REASON_BUDGET_EXHAUSTED,
        )
    return decision


def material_improvement_db() -> float:
    """The shipped bar for "an improvement worth applying", imported not copied.

    Public because the replay driver needs the same number to build a policy
    floor for model-graded metrics, and two import sites for one constant is
    how two constants start.

    :data:`~jasper.active_speaker.crossover_v2_flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`
    is 0.5 dB because that is the gap between what the correction model
    predicts and what the hardware realizes on JTS3 — an improvement smaller
    than the model's own error is not one that can be honestly claimed. The
    number is not restated here.

    Imported inside the function because ``crossover_v2_flow`` is the flow that
    will one day *call* this kernel; importing it at module scope would put a
    very large module (and its numpy/scipy weight) behind every import of a
    dependency-free decision kernel, and invite a cycle.
    """

    from jasper.active_speaker.crossover_v2_flow import (
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    )

    return float(PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB)


def replay(
    history: Sequence[AttemptRecord],
    floor: FloorStats,
    *,
    budget: AttemptBudget | None = None,
) -> list[LoopDecision]:
    """Every decision the loop would reach, one per attempt.

    A live loop stops at the first non-:data:`CONTINUE`. Replaying recorded
    attempts wants the whole walk — what the loop *would* have said at each
    step had it kept going — so an operator can see that a stop holds for the
    same reason at every subsequent attempt rather than only the first. The
    first non-``CONTINUE`` entry is where a live loop would actually have
    stopped; :func:`first_stop_index` finds it.
    """

    return [
        decide_next(history[: n + 1], floor, budget=budget)
        for n in range(len(history))
    ]


def first_stop_index(decisions: Sequence[LoopDecision]) -> int | None:
    """Index of the first decision a live loop would have stopped on."""

    for index, decision in enumerate(decisions):
        if decision.decision != CONTINUE:
            return index
    return None


def summarize(decisions: Sequence[LoopDecision]) -> Mapping[str, int]:
    """How many of each decision, for a report header."""

    counts = {name: 0 for name in sorted(DECISIONS)}
    for decision in decisions:
        counts[decision.decision] += 1
    return counts
