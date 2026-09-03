# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The S3 tuning loop's improve/stop policy — a pure decision kernel.

:func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` produces the
scalar the flat-linearization plan's S3 loop converges on and says, in its own
docstring, that "S3's loop policy — how much improvement counts, how many
iterations, when to stop — is not here and must not be." **This module is that
policy.** It is the consumer that instrument was landed ahead of.

It takes attempts that already carry their grades and returns one
:class:`LoopDecision`. Two claims about that, scoped exactly, because a claim
wider than its test is the kind of thing this module exists to refuse:

* **No I/O, ever.** Nothing here reads a file, opens a socket, or plays audio,
  on any path. Persistence lives next door in
  :mod:`jasper.active_speaker.model_error_store` precisely so this stays true
  and checkable rather than merely asserted.
* **Import-time lightness, not runtime lightness.** Importing this module pulls
  nothing but the standard library — pinned statically by
  ``test_kernel_imports_nothing_at_module_scope_but_stdlib``, which reads the
  module's own top-level imports. **Calling it is a different question.** The
  convergence check reaches :func:`material_improvement_db`, which
  deferred-imports :mod:`jasper.active_speaker.crossover_v2_flow` — and that
  pulls numpy and scipy. So one ``decide_next`` call, on the convergence path
  only, is not lightweight. That cost buys a single source of truth for the
  shipped 0.5 dB bar; copying the constant here to stay light would trade a
  real invariant for an import-graph nicety. The deferred import must stay
  inside the function: hoisting it to module scope would make the first claim
  false, and the static test fails if anyone does.

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

4. **A floor licenses only the separation it was measured across** —
   :attr:`FloorStats.scope`. The 2026-07-31 study is a *within-sitting* number:
   the mic was bolted in place and the repeats were ~21 s apart, so it bounds
   how far the instrument wanders while nothing moves. It says nothing about a
   pair separated by a microphone being put down and picked up again — the same
   README calls the remove/replace/re-aim arm unmeasured — **"arm" here is the
   statistics sense, one condition of a repeatability study, and is neither the
   measurement rig nor a DSP candidate under test** — and the panel's bound on
   it (3.2 dB) is an order of magnitude above the floor itself. So
   :func:`decide_next` refuses to grade a pair the floor's own scope does not
   cover (issue #2081), rather than reporting a claim the study never licensed.

The kernel grades a *magnitude* against that floor and, separately, asks
whether the change was an improvement. Both questions have to be answerable
before the loop may claim anything, and when either cannot be answered the
decision is :data:`STOP_EVIDENCE` — never a pass. That mirrors
:mod:`jasper.active_speaker.delta_probe`'s ``unavailable`` doctrine and
:class:`~jasper.active_speaker.flat_spec.BandResult`'s "unevaluable is a
first-class outcome, not a fabricated verdict".
"""

from __future__ import annotations

import math
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

#: The floor was derived across a separation no wider than ONE measurement
#: sitting — one continuous microphone placement — so it licenses only a pair
#: measured in the same sitting. This is what the 2026-07-31 study measured
#: (mic bolted in place, repeats ~21 s apart) and is the fail-closed value.
FLOOR_SCOPE_WITHIN_SITTING = "within_sitting"
#: The floor's derivation does not depend on the pair sharing a sitting —
#: either a re-placement study measured it that way, or the number is a policy
#: bar about something no microphone sits between.
FLOOR_SCOPE_ACROSS_SITTINGS = "across_sittings"

FLOOR_SCOPES: frozenset[str] = frozenset({
    FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPE_ACROSS_SITTINGS,
})

# Reasons. Free-form strings would drift between the kernel, the replay driver
# and the report; naming them here keeps one spelling per fact.
REASON_AWAITING_FIRST_ATTEMPT = "awaiting_first_attempt"
REASON_BASELINE_ESTABLISHED = "baseline_established"
REASON_ATTEMPT_NOT_COMPARABLE = "attempt_not_comparable"
REASON_PREDECESSOR_NOT_COMPARABLE = "predecessor_not_comparable"
REASON_FLOOR_METRIC_MISMATCH = "floor_metric_mismatch"
REASON_PROVENANCE_MISMATCH = "provenance_mismatch"
REASON_SITTING_MISMATCH = "sitting_mismatch"
REASON_SITTING_UNRECORDED = "sitting_unrecorded"
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


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile — NumPy's default method, in ten lines.

    Spelled out rather than imported so the floor's provenance is auditable
    without pinning a NumPy version: the claim floor is the product's most
    consequential decimal, and it should be reproducible by reading. Its
    agreement with the banked repeat-floor study's own published summary is
    pinned by ``tests/test_active_speaker_attempts_loop.py``.
    """

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile of an empty sample")
    if len(ordered) == 1:
        return ordered[0]
    position = (q / 100.0) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(low)]
    weight = position - low
    return ordered[int(low)] + weight * (ordered[int(high)] - ordered[int(low)])


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
      scope: which *separation* this floor was derived across —
        :data:`FLOOR_SCOPE_WITHIN_SITTING` or
        :data:`FLOOR_SCOPE_ACROSS_SITTINGS`. Orthogonal to :attr:`basis`, and
        the reason it is a second field rather than folded into the first: a
        measured floor and a policy bar can each cover either separation, so
        collapsing them would let "we measured this" imply "…across a mic
        re-placement", which is exactly the inference issue #2081 was filed
        about. The default is the fail-closed one: a floor that does not say
        which separation it covers licenses the narrow comparison only.
    """

    metric: str
    claim_floor_db: float
    basis: str
    source: str
    median_db: float | None = None
    p95_db: float | None = None
    measured_at: str = ""
    scope: str = FLOOR_SCOPE_WITHIN_SITTING

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("FloorStats.metric must be a non-empty name")
        if self.basis not in FLOOR_BASES:
            raise ValueError(f"unknown floor basis {self.basis!r}")
        if self.scope not in FLOOR_SCOPES:
            raise ValueError(f"unknown floor scope {self.scope!r}")
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
        scope: str = FLOOR_SCOPE_WITHIN_SITTING,
    ) -> "FloorStats":
        """A floor measured by repeating one unchanged measurement.

        ``claim_floor_db`` is computed as
        ``CLAIM_FLOOR_P95_MULTIPLE * p95_db`` — see that constant for why the
        multiple is 2 and why no decimal is transcribed here.

        ``scope`` defaults to :data:`FLOOR_SCOPE_WITHIN_SITTING` because that
        is what "repeat one unchanged measurement" describes, and the only such
        study banked (``captures/repeat-floor-20260731``) held the mic fixed.
        A study that DOES re-place the microphone between repeats passes
        :data:`FLOOR_SCOPE_ACROSS_SITTINGS` and the wider comparison is
        licensed with no kernel edit — which is the reason this is a parameter
        rather than a constant this constructor hardcodes.
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
            scope=scope,
        )

    @classmethod
    def from_policy_bar(
        cls, *, metric: str, claim_floor_db: float, source: str, scope: str,
    ) -> "FloorStats":
        """A shipped threshold standing in where no repeat study exists.

        Same arithmetic downstream, different epistemics: :attr:`median_db` and
        :attr:`p95_db` stay ``None`` rather than being back-solved from the
        bar, because inventing a p95 to fit a threshold would turn a policy
        choice into a fake measurement.

        ``scope`` is **required and has no default**, unlike
        :meth:`from_repeat_study`'s. A repeat study's separation is a fact
        about how it was run, so it can be inferred from the construction; a
        declared bar has no such fact behind it, and picking either default for
        it would be the module inventing the one thing the caller is the only
        one who knows. Making it answer is the point.
        """

        return cls(
            metric=metric,
            claim_floor_db=float(claim_floor_db),
            basis=FLOOR_BASIS_POLICY,
            source=source,
            scope=scope,
        )

    def licenses_sitting_pair(self, previous: str, latest: str) -> bool:
        """May a pair measured in these two sittings be graded against me?

        ``""`` is UNKNOWN, never a match, and that asymmetry is the whole
        guard: two unrecorded sittings compare equal as strings, so an
        ``==`` here would read a pair that cannot say where it came from as a
        pair that came from one place. State written before issue #2081 rides
        in with two blanks, which is precisely the case that must not pass.
        """

        if self.scope == FLOOR_SCOPE_ACROSS_SITTINGS:
            return True
        return bool(previous) and previous == latest

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "claim_floor_db": self.claim_floor_db,
            "basis": self.basis,
            "source": self.source,
            "median_db": self.median_db,
            "p95_db": self.p95_db,
            "measured_at": self.measured_at,
            "scope": self.scope,
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
      sitting_id: WHICH continuous microphone sitting produced this grade —
        opaque to the kernel, which only ever asks two of them whether they
        are the same string. ``""`` means unrecorded, and is treated as
        "not the same sitting" rather than as a match; see
        :meth:`FloorStats.licenses_sitting_pair`. Its sibling ``attempt_id``
        answers a different question and cannot stand in for it: two attempts
        necessarily carry two ids, so id inequality says nothing about whether
        the microphone moved between them.
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
        — that one is ``seed_ripple − committed_ripple``, what the (polarity,
        delay) objective bought over the correlation seed on a decision already
        taken, and plugging it in here would read a backward-looking number as
        a forward-looking one.
      in_spec: the fit is already inside spec; nothing left to chase.
      curve_refs: opaque pointers to per-band curves for the report. The kernel
        never dereferences them — it performs no I/O.
    """

    attempt_id: str
    metric: str
    provenance: str
    integrity: AttemptIntegrity
    sitting_id: str = ""
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
            "sitting_id": self.sitting_id,
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

    The pair must also fall inside the floor's own :attr:`FloorStats.scope` —
    rule 4. A within-sitting floor grades a within-sitting pair and nothing
    else, and "we do not know which sittings these came from" is refused on the
    same terms as "we know, and they differ".

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

    if not floor.licenses_sitting_pair(previous.sitting_id, latest.sitting_id):
        # Rule 4. Sits with the provenance refusal above and ahead of every
        # number below for the same reason that one does: both ask whether
        # these two grades may be subtracted AT ALL, and a magnitude computed
        # first would only be a threshold-shaped way of saying yes. The
        # distinction between the two reasons is operational, not cosmetic —
        # on the deploy that lands #2081 every persisted history carries no
        # sitting at all, and reading that as "the household moved the mic"
        # would send a maintainer looking for a household that did nothing
        # wrong. Such a speaker answers UNRECORDED until both attempts in its
        # pair were captured after the upgrade, and MISMATCH from then on;
        # neither is a claim, and the difference is which one a reader should
        # act on.
        return _decision(
            STOP_EVIDENCE,
            (
                REASON_SITTING_MISMATCH
                if previous.sitting_id and latest.sitting_id
                else REASON_SITTING_UNRECORDED
            ),
            basis_attempt_ids=pair,
            provenance=latest.provenance,
            notes=(
                f"floor scope {floor.scope}",
                f"previous sitting {previous.sitting_id or 'unrecorded'}",
                f"latest sitting {latest.sitting_id or 'unrecorded'}",
            ),
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

    :data:`~jasper.active_speaker.crossover_v2.attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`
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
