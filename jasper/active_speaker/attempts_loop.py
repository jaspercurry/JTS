# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The S3 tuning loop's improve/stop policy — a pure decision kernel.

:func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` computes the residual
but excludes loop policy by design; this module is that policy, consuming already-graded
attempts and returning one :class:`LoopDecision`.

No I/O ever (persistence lives in :mod:`model_error_store` instead); import-time light,
pulling only stdlib at module scope (pinned by
``test_kernel_imports_nothing_at_module_scope_but_stdlib``) -- the convergence path
alone deferred-imports numpy/scipy via :func:`material_improvement_db`, which must stay
a function-local import.

Four rules, all measured on jts3 2026-07-31
(``captures/repeat-floor-20260731/README.md``): (1) only consecutive attempts are
compared, never a fixed baseline (which drifts to roughly the whole floor within ~15
attempts); (2) repeat averaging stops paying past :data:`MAX_USEFUL_REPEAT_AVERAGES`;
(3) a change smaller than :attr:`FloorStats.claim_floor_db` is not a change; (4) a floor
licenses only the separation (:attr:`FloorStats.scope`) it was measured across --
refused otherwise (#2081).

An unanswerable magnitude or improvement question always yields :data:`STOP_EVIDENCE`,
never a pass, matching :mod:`delta_probe`'s ``unavailable`` doctrine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Sequence

#: Keep going: this attempt is gradeable and the loop has somewhere to go.
CONTINUE = "continue"
#: Nothing material is left to win, or the tune is already in spec.
STOP_CONVERGED = "stop_converged"
#: Change from the predecessor is smaller than the instrument can resolve.
STOP_FLOOR = "stop_floor"
#: The attempt budget is spent.
STOP_BUDGET = "stop_budget"
#: The evidence does not support grading this attempt at all. Never a pass.
STOP_EVIDENCE = "stop_evidence"

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

#: Derived across a separation no wider than ONE measurement sitting; licenses
#: only a pair in the same sitting. The fail-closed value (2026-07-31 study:
#: mic bolted in place, repeats ~21 s apart).
FLOOR_SCOPE_WITHIN_SITTING = "within_sitting"
#: Derivation does not depend on the pair sharing a sitting.
FLOOR_SCOPE_ACROSS_SITTINGS = "across_sittings"

FLOOR_SCOPES: frozenset[str] = frozenset({
    FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPE_ACROSS_SITTINGS,
})

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

#: The honest per-attempt claim floor is **twice** the observed p95 of
#: consecutive-pair deviations (two-sided: both attempts in a pair carry
#: that error independently). ``captures/repeat-floor-20260731/README.md``
#: states "≥ 0.2 dB ... 2x the consecutive-pair p95 (0.085 dB)" -- the same
#: rule at display rounding (p95 0.08508 dB -> 0.17016 dB); the kernel
#: computes the unrounded value.
CLAIM_FLOOR_P95_MULTIPLE = 2.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, NumPy's default method, spelled out (not imported) so
    the floor's provenance is auditable without pinning a NumPy version; pinned against
    the banked study's own summary by ``tests/test_active_speaker_attempts_loop.py``.
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


#: Averaging more than four repeats buys resolution the box's drift
#: immediately spends. Measured 2026-07-31: sigma falls as 1/sqrt(M), drift
#: accumulates linearly across the ~21 s per repeat, and at M=4 the two cross
#: (sigma/sqrt(M) ~= 0.014 dB vs ~= 0.018 dB accumulated drift). Numerically
#: equal to, but distinct in meaning from,
#: :data:`~jasper.active_speaker.repeat_admission.MAX_ATTEMPTS` (a spend
#: budget) and
#: :data:`~jasper.active_speaker.commissioning_capture.DEFAULT_REPEAT_TARGET`
#: (outlier rejection, not noise-floor reduction) -- do not unify.
MAX_USEFUL_REPEAT_AVERAGES = 4


@dataclass(frozen=True)
class AttemptBudget:
    """How many *tuning attempts* one speaker gets (a tune-and-grade cycle, not a measurement
    repeat). ``target_attempts`` is the planning number, disclosed not enforced --
    the only bound the kernel applies is ``hard_cap_attempts``. ``hard_cap_attempts`` is
    a policy bound (target plus one retry); no measurement sets it, and it is a
    different quantity from :data:`MAX_USEFUL_REPEAT_AVERAGES`.
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
    """What a change in ``metric`` has to clear before the loop may claim it. Build with
    :meth:`from_repeat_study` (measured) or :meth:`from_policy_bar` (no study);
    :attr:`basis` rides through every decision. :attr:`scope` (separation covered) is
    orthogonal to :attr:`basis` -- collapsing them would let "we measured this" imply
    "across a mic re-placement" (#2081); ``median_db``/``p95_db`` are ``None`` on a
    policy bar. :func:`decide_next` refuses to grade an attempt whose ``metric``
    differs.
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
        """A floor measured by repeating one unchanged measurement. ``claim_floor_db`` is
        ``CLAIM_FLOOR_P95_MULTIPLE * p95_db``. ``scope`` defaults to
        :data:`FLOOR_SCOPE_WITHIN_SITTING` (the only banked study held the mic fixed); a
        re-placement study passes :data:`FLOOR_SCOPE_ACROSS_SITTINGS`.
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
        :attr:`median_db`/:attr:`p95_db` stay ``None`` rather than being back-solved.
        ``scope`` is required, with no default: a declared bar has no construction fact
        to infer it from.
        """

        return cls(
            metric=metric,
            claim_floor_db=float(claim_floor_db),
            basis=FLOOR_BASIS_POLICY,
            source=source,
            scope=scope,
        )

    def licenses_sitting_pair(self, previous: str, latest: str) -> bool:
        """May a pair measured in these two sittings be graded against me? ``""`` is UNKNOWN, never
        a match: two unrecorded sittings must not compare equal as if they were the same
        place (pre-#2081 state rides in with two blanks).
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
    """Whether this attempt's grade may be compared to another one at all. ``comparable`` is
    the shipped acceptance gate's answer, not a quality score. ``reasons`` carries the
    gate's own reason strings through unchanged.
    """

    comparable: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"comparable": self.comparable, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class AttemptRecord:
    """One tuning attempt, already graded. Grades are lower-is-better throughout; the kernel
    carries no direction flag. Two grade shapes are accepted: ``grade_db`` (absolute,
    gives magnitude and direction) and ``deviation_from_predecessor_db`` (unsigned
    magnitude only); when both are present the deviation wins for magnitude, grades
    still supply direction. ``provenance`` may not be compared across
    :data:`PROVENANCE_MODEL_GRADED`/:data:`PROVENANCE_REALIZED`. ``sitting_id`` is
    opaque (only ever string-compared); ``""`` means unrecorded, treated as "not the
    same sitting" (see :meth:`FloorStats.licenses_sitting_pair`). ``n_graded_bins`` is
    enforced asymmetrically: a *grown* denominator only makes an improvement harder to
    win. ``predicted_remaining_improvement_db`` is NOT
    ``CrossoverCandidate.flatness_improvement_db`` (backward-looking, over a decision
    already taken). ``curve_refs`` are never dereferenced here -- the kernel performs no
    I/O.
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
    """What the loop decided, and every number it decided from. ``improved`` is carried
    separately from ``decision``: an above-floor *regression* is still :data:`CONTINUE`
    (the loop keeps working), never approval -- ``improved=False`` plus
    :data:`REASON_REGRESSION_FROM_PREDECESSOR` says so. Handling a regression is the
    live flow's policy, not this kernel's.
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
    """The size of the change, and how much of it was an improvement. Improvement is
    ``previous.grade_db - latest.grade_db`` (positive = better), ``None`` if either is
    absent. Magnitude prefers the comparator's own deviation when supplied, since that
    number was measured rather than subtracted.
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
    """Improve or stop, given the attempts so far. ``history`` is oldest-first; only its last
    two entries are read (rule 1), with no ``baseline=`` parameter or way to reach
    further back -- a fixed baseline accumulates drift comparable to the entire floor
    within ~15 attempts. An incomparable predecessor refuses to grade. The pair must
    also fall inside the floor's own :attr:`FloorStats.scope` (rule 4). Order of
    judgement: evidence first, then the floor, then (only if otherwise :data:`CONTINUE`)
    convergence, then budget.
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
        # Rule 4. On the deploy that lands #2081 every persisted history
        # carries no sitting yet, so this answers UNRECORDED (not MISMATCH)
        # until both attempts in the pair postdate the upgrade.
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
        # `improved` stays None: a sub-floor change is unresolvable, not a
        # regression.
        return _graded(STOP_FLOOR, REASON_BELOW_CLAIM_FLOOR)

    if improvement_db is None:
        # Above floor but direction unknown; may not be reported as improved.
        return _graded(STOP_EVIDENCE, REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR)

    improved = improvement_db > 0.0
    if improved and _denominator_shrank(previous, latest):
        # A grade pooled over fewer bins falls for free.
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
    """Turn a would-be :data:`CONTINUE` into a stop when there is nowhere to go. Only reachable
    from a decision that already survived the evidence and floor checks.
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
    """The shipped bar for "an improvement worth applying", imported not copied. 0.5 dB is
    the gap between what the correction model predicts and what JTS3's hardware
    realizes. Imported inside the function to keep numpy/scipy off this dependency-free
    kernel's import path.
    """

    from jasper.active_speaker.crossover_v2_flow import (
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    )

    return float(PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB)
