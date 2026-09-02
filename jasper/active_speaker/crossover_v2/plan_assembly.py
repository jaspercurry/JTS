# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The composed prediction of a linearized branch sum, and the plan it lands in.

Dependency direction: this module reads :mod:`.contracts`;
:mod:`.intervention` imports it, never the reverse.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from ..branch_chain import (
    HEADROOM_MARGIN_DB,
    CrossoverSection,
    branch_chain_peak_db,
    chain_response,
    headroom_charge_db,
)
from jasper.audio_measurement.program_analysis import (
    RealizedLevelMatch,
    predicted_branch_sum,
)

from .contracts import LINEARIZATION_OUTCOME_SINGLE_BRANCH, TrimStrategy, detached_json

__all__ = [
    "FittedBranches",
    "JournalRecord",
    "LevelConsistency",
    "LinearizationPlan",
    "SummationFrame",
    "TrimDecision",
    "assemble_plan",
    "compose_linearized_prediction",
]


@dataclass(frozen=True, init=False)
class JournalRecord:
    """One log line the planner would have emitted, as data.

    The planner owns *what happened*; the host owns *how it is said* — it adds
    its own ``session_id`` and writes through its own logger. Returned in
    emission order.

    A consumer cannot reach the plan through a record: the payload is detached
    at construction by :func:`~.contracts.detached_json`, and :attr:`fields` is
    a property returning a fresh detached copy on every read, so a formatter
    that pops a key, clears the mapping, or edits a nested value edits a
    throwaway. Copy-on-read rather than ``MappingProxyType`` because a proxy is
    not JSON-serializable — the ``JASPER_LOG_JSON=1`` sink is
    ``json.dumps(..., default=str)``, so a nested mapping would degrade from a
    real object to a quoted Python repr.
    """

    event: str
    level: int
    _fields: Mapping[str, Any] = field(repr=False)

    def __init__(
        self, event: str, fields: Mapping[str, Any], level: int = logging.INFO
    ) -> None:
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "_fields", detached_json(dict(fields)))

    @property
    def fields(self) -> Mapping[str, Any]:
        """This record's payload — a fresh copy each call; see the class doc."""

        return detached_json(self._fields)


@dataclass(frozen=True)
class LevelConsistency:
    """How far apart the two level DEFINITIONS place the pair, and on which axis.

    A disclosure, not a placement. The pair is anchored on the raw measured
    trim (:func:`anchor_trims`), so neither number places anything — and the
    two were never estimates of one quantity, so a gap between them is a fact
    about two definitions, not a fault in either.

    ``None`` in place of this value means **one of the two definitions covered
    no role**, so there was nothing to report — a third state, and not a quiet
    synonym for "they landed together".
    """

    differs: bool
    """Did the gap, on either role, exceed the disclosure trigger?"""

    reason: str
    """:data:`LEVEL_DEFINITIONS_DIFFER_REASON` when it differs, ``""`` when not."""

    tolerance_db: float

    worst_delta_db: float
    """The largest single per-role gap between the two definitions, in dB."""

    estimator_delta_db: Mapping[str, float]
    """Per role: |handover placement − passband placement|, in dB."""

    matched_axis: str
    """:data:`LEVEL_MATCH_AXIS` — WHICH axis these levels were read on."""

    def to_dict(self) -> dict[str, Any]:
        """This verdict as the JOURNAL's payload — its only caller.

        Deliberately not a shared serialization: the banked finding needs these
        numbers flat and role-suffixed and builds them itself off the
        attributes. One verdict owner, one adapter per surface.
        """

        return {
            "differs": bool(self.differs),
            "reason": self.reason,
            "matched_axis": self.matched_axis,
            "tolerance_db": round(float(self.tolerance_db), 3),
            "worst_delta_db": round(float(self.worst_delta_db), 3),
            "estimator_delta_db": {
                role: round(float(value), 3)
                for role, value in self.estimator_delta_db.items()
            },
        }


@dataclass(frozen=True)
class TrimDecision:
    """Which trim pair was committed, and the complete evidence for why."""

    committed_db: Mapping[str, float]
    strategy: TrimStrategy
    rationale: str
    anchored_db: Mapping[str, float]
    resolved_db: Mapping[str, float]
    anchor_drift_db: float
    sanity_margin_db: float
    beyond_sanity_margin: bool
    committed_match: RealizedLevelMatch
    ripple_db: float | None
    """The scan's own ripple at its optimum; ``None`` when the scan was skipped."""

    @property
    def committed_side(self) -> str:
        """``"anchored"`` or ``"resolved"`` — derived from the strategy.

        The journal's own ``committed`` field reads this rather than a literal,
        so "which pair won" has one owner and no second copy to drift from it.
        """
        return (
            "resolved"
            if self.strategy
            in (
                TrimStrategy.RESOLVED_COMMITTED,
                TrimStrategy.RESOLVED_COMMITTED_AFTER_SANITY_DRIFT,
            )
            else "anchored"
        )

    @property
    def outcome(self) -> str:
        """The persisted ``linearization_outcome`` string.

        ``"trim_rejected"`` is literal: a beyond-margin scan IS rejected, and
        the anchor is what ships.
        """
        return "trim_rejected" if self.beyond_sanity_margin else "fitted"


@dataclass(frozen=True)
class SummationFrame:
    """The RAW branches, and the frame a prediction of their sum is taken in.

    Everything :func:`compose_linearized_prediction` needs except the filters
    and the trims — held together because a prediction composed from one
    round's branches in another round's frame is a number with no meaning.

    ``residual_delay_us`` is already the RESIDUAL relative to the
    argmax-referenced frame (:func:`summed_model_residual_delay_us`'s answer),
    never the applied delay — passing the applied delay double-counts the
    measured peak gap.

    One or two branches: ``polarity_sign`` and ``residual_delay_us`` describe
    how a SECOND branch lands against the first, so they cannot go N-ary.
    """

    freqs_hz: np.ndarray
    branch_tf: Mapping[str, np.ndarray]
    """Role -> its RAW measured transfer function, lowest branch first."""
    polarity_sign: int
    residual_delay_us: float


def compose_linearized_prediction(
    frame: SummationFrame,
    *,
    filters_by_role: Mapping[str, Sequence[Mapping[str, Any]]],
    role_attenuations_db: Mapping[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    """A model of exactly what the emitted graph will do, as ``(freqs, dB)``.

    THE composition: a second implementation of this arithmetic is how a
    prediction and a graph drift apart.

    The correction is COMPLEX (minimum-phase), not a zero-phase magnitude
    scale: the emitted biquads rotate phase near their corners and this
    summation is phase-dominated, so a magnitude-only model mistracked the
    measured VERIFY summation by ~2.0 dB — WORSE than modelling no correction
    at all — where the complex model tracks to ~0.5 dB.

    ONE grid for both branches, a precondition rather than a simplification:
    the branch product and the sum below are elementwise.

    ``filters_by_role`` is the PERSISTED filter shape
    (``{role: [{biquad_type, freq, q, gain}, ...]}``) — what the emitter
    re-validates — so both callers speak one shape. A role with no entry is
    corrected by unity: :func:`~..branch_chain.chain_response` returns ones for
    an empty filter list, so an unfitted, unprescribed branch survives raw.
    """
    roles = tuple(frame.branch_tf)
    corrected = [
        frame.branch_tf[role]
        * chain_response(
            [dict(f) for f in filters_by_role.get(role, ())],
            frame.freqs_hz,
        )
        for role in roles
    ]
    if len(corrected) == 1:
        predicted = corrected[0] * 10.0 ** (
            float(role_attenuations_db[roles[0]]) / 20.0
        )
    else:
        predicted = predicted_branch_sum(
            corrected[0],
            corrected[1],
            role_attenuations_db[roles[0]],
            role_attenuations_db[roles[1]],
            int(frame.polarity_sign),
            freqs_hz=frame.freqs_hz,
            residual_delay_us=frame.residual_delay_us,
        )
    return (
        frame.freqs_hz,
        20.0 * np.log10(np.maximum(np.abs(predicted), 1e-12)),
    )


@dataclass(frozen=True)
class LinearizationPlan:
    """One candidate's complete prescription, as a value."""

    fc_hz: float | None
    """This candidate's corner; ``None`` on a 1-way main, which declares none."""
    role_attenuations_db: Mapping[str, float]
    linearization: Mapping[str, Any]
    trim: TrimDecision | None
    """The committed inter-driver trim pair; ``None`` on a 1-way main."""
    core_level_evidence: Mapping[str, Mapping[str, Any]]
    """Per role: the fit's core-band median, and the two bands behind it.

    Subordinate evidence and the banked finding's per-role provenance, never a
    placement.
    """
    trim_band_estimate_db: Mapping[str, float]
    """Per role: the trim solve's own level-match term. The other subordinate."""
    polish_delta_db: Mapping[str, float]
    """Per role: how far the base the anchor was handed sits from THIS plan's
    own band-average solve — the MEASURE path's ripple polish, measured here
    rather than taken on trust.

    Zero on the ordinary round. Non-zero means the give-back is calibrated to a
    base that moved, and the excursion passes straight through to the committed
    pair as realized inter-driver level error. A plan field rather than only a
    journal field because the realized-level disclosure reads it as its own
    attribution; see :func:`~.accountability.assess_accountability`.
    """
    level_consistency: LevelConsistency | None
    """The two per-driver level estimates, graded against each other.

    ``None`` when one of the two estimates covered no role. See
    :func:`~.intervention.compare_level_definitions`. It discloses a
    difference; it never moves this candidate's anchor and cannot.
    """
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray]
    summation_frame: SummationFrame
    """The RAW material :attr:`linearized_predicted_sum` was composed from.

    Carried so a caller that changes which filters will actually ship can
    recompose the prediction through :func:`compose_linearized_prediction` —
    the one composition — instead of shipping a prediction of a graph nobody
    will emit. Its only such caller is :func:`~.planning.build_candidate`'s
    per-driver prescription merge.
    """
    radiating_band_hz: Mapping[str, tuple[float, float]]
    journal: tuple[JournalRecord, ...] = field(default=())
    journal_dropped: tuple[str, ...] = field(default=())
    """Records the host's disclosure port refused, as ``event: Error: detail``.

    Empty is the ordinary case. Non-empty means the plan is sound but the
    host's own logging lost lines. A plan FIELD rather than a final journal
    record on purpose: a record announcing that the port is failing would be
    emitted through the failing port.
    """

    @property
    def outcome(self) -> str:
        """The persisted ``linearization_outcome`` string.

        No trim decision means no PAIR existed to decide one for: its own value
        rather than a bare ``"fitted"``.
        """
        if self.trim is None:
            return LINEARIZATION_OUTCOME_SINGLE_BRANCH
        return self.trim.outcome

    @property
    def realized_level_match(self) -> RealizedLevelMatch | None:
        return None if self.trim is None else self.trim.committed_match


@dataclass(frozen=True)
class FittedBranches:
    """What the fit produced, before any trim was settled."""

    fc_hz: float | None
    fits: Mapping[str, Any]
    sections: Mapping[str, tuple[CrossoverSection, ...]]
    radiating_band_hz: Mapping[str, tuple[float, float]]
    core_level_evidence: Mapping[str, Mapping[str, Any]]
    trim_band_estimate_db: Mapping[str, float]
    level_consistency: LevelConsistency | None


def assemble_plan(
    fitted: FittedBranches,
    *,
    role_attenuations_db: Mapping[str, float],
    trim: TrimDecision | None,
    polish_delta_db: Mapping[str, float],
    summation_frame: SummationFrame,
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray],
    emit: Callable[[str, Mapping[str, Any]], None],
    records: Sequence[JournalRecord],
    dropped: Sequence[str],
) -> LinearizationPlan:
    """Charge the fitted chains and assemble the plan.

    ``records`` is passed live: it is snapshotted AFTER the headroom record.
    """
    # The headroom charge, computed now that the trim is committed.
    #
    # A correction's cost is a property of the CHAIN it is emitted into — the
    # crossover that follows it and the trim that follows that — so the
    # topology-agnostic fit core cannot compute it. This is the same
    # ``branch_headroom_db`` the emitter charges ``active_baseline_headroom``
    # with, over the same three terms, so the number the household is told and
    # the number the speaker gives up are one number.
    #
    # ``role_attenuations_db`` and not ``anchored``/``resolved``: whichever pair
    # the trim decision committed is the trim the graph will run, and the charge
    # follows the emitted graph.
    #
    # ``normalize_shift_db`` is NET-NEUTRAL under this rule. Subtracting a
    # common S from every branch's trim lowers every branch's chain peak by S,
    # so it lowers this charge by S too, and the pre-split attenuation the
    # emitter applies falls by the same S the branches gained: identical output,
    # no lost loudness. The one non-neutral case is a branch whose whole chain
    # already sits below unity — the charge floors at 0 and cannot fall further
    # — which the ``normalize_shift_db`` field still discloses.
    charge_db: dict[str, float] = {}
    peak_db: dict[str, float] = {}
    linearization: dict[str, Any] = {}
    for role, fit in fitted.fits.items():
        emitted = [f.to_dict() for f in fit.filters]
        trim_db = float(role_attenuations_db.get(role, 0.0))
        peak_db[role] = branch_chain_peak_db(
            emitted, sections=fitted.sections[role], trim_db=trim_db
        )
        charge_db[role] = headroom_charge_db(peak_db[role])
        linearization[role] = replace(fit, headroom_cost_db=charge_db[role]).to_dict()
    emit(
        "correction.crossover_v2_linearization_headroom",
        {
            # What the branch actually puts above unity at its loudest bin...
            "chain_peak_db": {r: round(v, 3) for r, v in peak_db.items()},
            # ...what that costs the speaker's maximum level (peak + margin,
            # 0.0 for a chain that never exceeds unity)...
            "headroom_cost_db": {r: round(v, 3) for r, v in charge_db.items()},
            # ...and what a sum-of-positives rule would charge instead, kept so
            # the reclaimed loudness is visible in the journal.
            "sum_of_positives_db": {
                role: round(math.fsum(f.gain for f in fit.filters if f.gain > 0.0), 3)
                for role, fit in fitted.fits.items()
            },
            "trim_db": {
                role: round(float(role_attenuations_db.get(role, 0.0)), 3)
                for role in fitted.fits
            },
            "margin_db": HEADROOM_MARGIN_DB,
        },
    )

    return LinearizationPlan(
        fc_hz=fitted.fc_hz,
        role_attenuations_db=role_attenuations_db,
        linearization=linearization,
        trim=trim,
        core_level_evidence=fitted.core_level_evidence,
        trim_band_estimate_db=fitted.trim_band_estimate_db,
        polish_delta_db=dict(polish_delta_db),
        level_consistency=fitted.level_consistency,
        linearized_predicted_sum=linearized_predicted_sum,
        summation_frame=summation_frame,
        radiating_band_hz=fitted.radiating_band_hz,
        journal=tuple(records),
        journal_dropped=tuple(dropped),
    )
