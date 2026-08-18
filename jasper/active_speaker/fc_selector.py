"""R17's crossover-Fc selector: scoring and adjudication, as pure functions.

It reads per-candidate evidence gathered at MEASURE-consume, scores each
candidate against the lateral walk's robustness evidence, and returns a
RECOMMENDATION. The host retains its winner for Sound-owned apply; see R17.

**No stage-1 session feeds this today.** The lateral walk that produces
``poses`` was paused on 2026-08-18 (``crossover_v2_flow.
STAGE1_INCLUDES_LATERAL``, whose comment carries the evidence), and the sweep
that gathers the evidence fires only in a session that walks — so the shipped
round commits its configured Fc without scoring candidates. Nothing here
changed and nothing here is dead: forcing that flag back on restores this
module's inputs unmodified. It is kept intact for the redesigned lateral
statistic the pause is waiting on.

Everything here is a pure function of small arrays — no conductor state, no
I/O. ``M`` below is a pose's NEUTRAL measured transfer (``plant * P``), what
``LateralPoseCurve.complex_tf`` holds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: How much better an alternative must score before it is recommended at all
#: (§9.8: configured is golden until a candidate proves equivalence and THEN
#: improvement). Not a taste knob — the 2026-08-05 checkpoint's own dip spread
#: -3.8...-6.9 dB (sd 1.28) across five positions plus the mark, so a smaller
#: margin would recommend a change below the speaker's own disagreement.
MIN_RECOMMENDATION_MARGIN_DB = 1.0

#: Weight on how much worse the handoff gets OFF the design axis. Lateral
#: evidence is a COARSE gate (#1968) — six poses at 1/12 octave, not a polar
#: measurement — so it discounts rather than dominates.
LATERAL_ROBUSTNESS_WEIGHT = 0.5

#: Weight on headroom given up: real but recoverable (turn it up), unlike a
#: handoff notch.
HEADROOM_COST_WEIGHT = 0.25

#: Why a candidate could not be scored — each operator- or household-actionable.
EVAL_REFUSED_UNFITTABLE = "fit_refused"
EVAL_REFUSED_NO_BAND = "no_trusted_crossover_region"
EVAL_REFUSED_BUDGET = "evaluation_budget_spent"

SELECTION_KEEP_CONFIGURED = "keep_configured"
SELECTION_RECOMMEND = "recommend_alternative"
SELECTION_NOTHING_TO_COMPARE = "no_alternative_evaluated"


@dataclass(frozen=True)
class BandFlatness:
    """Signed worst deviation from flat across one band, plus its rms.

    R18's arithmetic, borrowed deliberately (``_verify_absolute_result``): the
    deviation is MEAN-REMOVED before the worst bin is taken, so this answers
    "how much does the response wobble through the handoff", not "where does
    its level sit" — level is the trim solve's business. ``worst_db`` keeps its
    SIGN because a dip and a peak are opposite defects with opposite causes.
    """

    band_hz: tuple[float, float]
    worst_db: float
    worst_hz: float
    rms_db: float
    n_bins: int

    @property
    def penalty_db(self) -> float:
        """The scoreable magnitude — flatness costs in either direction."""
        return abs(self.worst_db)


@dataclass(frozen=True)
class FcCandidateEvaluation:
    """One candidate's retained result — the ONLY thing that crosses the walk.

    **This type is the memory contract.** The #1894 ruling caps the selector at
    one analysis + 50 MB by requiring each candidate's intermediates (analysis,
    driver responses, envelopes — hundreds of MB) to be released before the
    next starts. Bounded evidence retains the winner, not a ``ProgramAnalysis``,
    ``DriverResponse`` or ``EnvelopeCurve``.

    The guard is
    ``test_crossover_v2_fc_selector_wiring.test_the_sweep_retains_no_analysis_sized_object``,
    which walks the fields of records a REAL sweep produced and whitelists them
    by type — so a new field of an unexpected type fails by construction. It is
    named here rather than the kernel suite's shape test because that one
    inspects a hand-built instance: a field populated only in production would
    never be set on it, which the resilience lens demonstrated by hoarding a
    full analysis past a green suite.

    ``branch_operator_by_role`` carries everything a candidate would do to one
    driver's neutral measurement — polarity, the ``C_c / P`` re-composition,
    the fitted correction, trim and delay — so a pose sum is one multiply-add.

    ``refusal`` (an ``EVAL_REFUSED_*`` code) means not scoreable; it rides the
    disclosure so a skipped candidate is stated, never silently dropped.
    """

    fc_hz: float
    freqs_hz: np.ndarray
    branch_operator_by_role: Mapping[str, np.ndarray]
    anchor_sum_db: np.ndarray
    scoring_band_hz: tuple[float, float] | None
    headroom_cost_db: float = 0.0
    refusal: str | None = None
    candidate: Mapping[str, Any] | None = None
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    predicted_spec_report: Mapping[str, Any] | None = None
    commanded_delta: tuple[np.ndarray, np.ndarray] | None = None
    #: The delta probe's STATE axis for THIS candidate (#2614): the applied
    #: graph's own transfer against the uncorrected crossover, which is what the
    #: two directional safety rules mask on. Carried beside ``commanded_delta``
    #: and in the same already-decimated-at-persist shape, because the stage
    #: that grades is not the stage that fit.
    declared_transfer: tuple[np.ndarray, np.ndarray] | None = None
    level_frame_finding: Mapping[str, Any] | None = None
    realized_branch_level: Mapping[str, Any] | None = None
    """THIS candidate's own realized inter-driver level verdict, serialized.

    Carried since #2291 Phase 2b, and the reason it can be is the cutover
    itself: the planner returns its verdict as a value, so the sweep no longer
    saves and restores a conductor scratch field around each candidate. Before
    that, the only realized-level verdict reachable at selection time belonged
    to the ANCHOR — reading it would have described a different candidate — so
    a selected alternative corner reached its proposal with the field empty
    (#2307 gate note N6).

    A ``Mapping`` rather than the ``RealizedLevelMatch`` value on purpose: this
    type is the sweep's memory contract and its retention guard whitelists
    field types, so evidence crosses the walk in the same already-serialized
    shape ``level_frame_finding`` uses.
    """

    @property
    def scoreable(self) -> bool:
        return self.refusal is None and self.scoring_band_hz is not None


@dataclass(frozen=True)
class FcCandidateScore:
    """One candidate's score and the components that produced it."""

    fc_hz: float
    anchor: BandFlatness
    lateral_worst: BandFlatness | None
    lateral_excess_db: float
    headroom_cost_db: float
    score: float
    n_poses: int


@dataclass(frozen=True)
class FcSelection:
    """The selector's verdict — a recommendation, never an instruction.

    ``kept_configured`` is an HONEST verdict (§9.8): returned when nothing was
    proposable, when nothing cleared the margin, and — today, on the live jts3
    declaration — when the tweeter's declared band admits no alternative.

    ``comparison_complete`` is the one completeness fact: true exactly when
    every deliberately selected candidate was attempted. Invalid attempted
    candidates remain refusals but do not make the comparison incomplete;
    budget-skipped candidates do. An incomplete comparison can keep the safe
    configured path, but can never recommend an alternative.
    """

    verdict: str
    configured_hz: float
    recommended_hz: float | None
    margin_db: float | None
    scores: tuple[FcCandidateScore, ...]
    refusals: tuple[tuple[float, str], ...]
    limits: Mapping[str, float]
    evaluated: int
    planned: int
    candidate_order: tuple[float, ...]
    attempted: tuple[float, ...]
    skipped: tuple[tuple[float, str], ...]
    comparison_complete: bool

    @property
    def kept_configured(self) -> bool:
        return self.verdict != SELECTION_RECOMMEND


def fc_comparison_complete(
    evaluations: Sequence[FcCandidateEvaluation], planned: int,
) -> bool:
    """Whether every deliberately selected candidate was attempted."""
    return len(evaluations) == int(planned) and all(
        e.refusal != EVAL_REFUSED_BUDGET for e in evaluations
    )


def band_flatness(
    freqs_hz: np.ndarray,
    magnitude_db: np.ndarray,
    band_hz: tuple[float, float] | None,
) -> BandFlatness | None:
    """Mean-removed signed worst deviation over ``band_hz``, or ``None``.

    ``None`` when the band is absent or holds no finite bins — no number is
    invented for a band this evidence does not support.
    """
    if band_hz is None:
        return None
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    mask = (freqs >= float(band_hz[0])) & (freqs <= float(band_hz[1]))
    if not np.any(mask):
        return None
    inside = np.asarray(magnitude_db, dtype=np.float64)[mask]
    finite = np.isfinite(inside)
    if not np.any(finite):
        return None
    band_freqs, inside = freqs[mask][finite], inside[finite]
    deviation = inside - float(np.mean(inside))
    worst = int(np.argmax(np.abs(deviation)))
    return BandFlatness(
        band_hz=(float(band_hz[0]), float(band_hz[1])),
        worst_db=float(deviation[worst]),
        worst_hz=float(band_freqs[worst]),
        rms_db=float(np.sqrt(np.mean(deviation**2))),
        n_bins=int(deviation.size),
    )


def predict_pose_sum_db(
    evaluation: FcCandidateEvaluation, curves: Sequence[Any],
) -> np.ndarray | None:
    """This candidate's predicted design-axis sum AT ONE POSE, in dB.

    ``S_pose = sum_role M_pose,role * operator_role`` — §4.2's composition
    applied to the pose's own neutral measurement, the step ``_lateral_priors``
    deliberately left to the consumer.

    ``curves`` are duck-typed on ``role`` / ``complex_tf`` / ``band_hz`` so this
    module does not import the flow. ``None`` unless EVERY role with an
    operator is present: §4.4's questions are all woofer-versus-HF comparisons,
    which a half-measured pose cannot answer. Bins outside a role's driven band
    contribute nothing — no stimulus there, so the sample is noise.
    """
    if not evaluation.branch_operator_by_role:
        return None
    by_role = {str(curve.role): curve for curve in curves}
    total = np.zeros(evaluation.freqs_hz.shape, dtype=np.complex128)
    for role, operator in evaluation.branch_operator_by_role.items():
        curve = by_role.get(role)
        if curve is None:
            return None
        measured = np.asarray(curve.complex_tf, dtype=np.complex128)
        if measured.shape != total.shape:
            return None
        lo, hi = (float(edge) for edge in curve.band_hz)
        driven = (evaluation.freqs_hz >= lo) & (evaluation.freqs_hz <= hi)
        total = total + np.where(driven, measured * operator, 0.0)
    return 20.0 * np.log10(np.maximum(np.abs(total), 1e-12))


def score_candidate(
    evaluation: FcCandidateEvaluation, poses: Sequence[Sequence[Any]],
) -> FcCandidateScore | None:
    """Score one candidate. Lower is better. ``None`` if not scoreable.

    Three components: design-axis handoff flatness at the mark (the primary
    term — the checkpoint's measured -4.80 dB at 1656 Hz was exactly this);
    lateral robustness at :data:`LATERAL_ROBUSTNESS_WEIGHT`, as the EXCESS over
    the design axis because every candidate pays the room's own off-axis cost
    and only the difference is the crossover's, clamped at zero so a coarse
    gate never earns credit; and headroom at :data:`HEADROOM_COST_WEIGHT`.

    **The ka/beaming prior is deliberately NOT a term.** Per PR-1's settled
    collision policy it bounds what may be PROPOSED, while the configured Fc is
    always evaluated even above the ceiling (jts3: 1915.4 ceiling, 2000
    configured). Re-applying it here would charge the configured candidate
    twice for a bound the records call guidance, and could recommend a change
    on the strength of a prior rather than a measurement.
    """
    if not evaluation.scoreable:
        return None
    anchor = band_flatness(
        evaluation.freqs_hz, evaluation.anchor_sum_db, evaluation.scoring_band_hz
    )
    if anchor is None:
        return None
    lateral_worst: BandFlatness | None = None
    n_poses = 0
    for curves in poses:
        predicted = predict_pose_sum_db(evaluation, curves)
        if predicted is None:
            continue
        flatness = band_flatness(
            evaluation.freqs_hz, predicted, evaluation.scoring_band_hz
        )
        if flatness is None:
            continue
        n_poses += 1
        if lateral_worst is None or flatness.penalty_db > lateral_worst.penalty_db:
            lateral_worst = flatness
    excess = (
        max(0.0, lateral_worst.penalty_db - anchor.penalty_db)
        if lateral_worst is not None
        else 0.0
    )
    headroom = max(0.0, float(evaluation.headroom_cost_db))
    return FcCandidateScore(
        fc_hz=float(evaluation.fc_hz),
        anchor=anchor,
        lateral_worst=lateral_worst,
        lateral_excess_db=excess,
        headroom_cost_db=headroom,
        score=(
            anchor.penalty_db
            + LATERAL_ROBUSTNESS_WEIGHT * excess
            + HEADROOM_COST_WEIGHT * headroom
        ),
        n_poses=n_poses,
    )


def select_fc(
    evaluations: Sequence[FcCandidateEvaluation],
    poses: Sequence[Sequence[Any]],
    *,
    configured_hz: float,
    limits: Mapping[str, float],
    planned: int,
    margin_db: float = MIN_RECOMMENDATION_MARGIN_DB,
) -> FcSelection:
    """Adjudicate the evaluated candidates into one recommendation.

    §9.8 as code: the configured candidate is always in the evaluated set, an
    alternative must beat it by ``margin_db``, and keeping configured is an
    honest verdict. Ties go to configured — the burden of proof is on the
    change, which still asks the household to accept and verify another apply.
    """
    scores: list[FcCandidateScore] = []
    refusals: list[tuple[float, str]] = []
    candidate_order = tuple(float(e.fc_hz) for e in evaluations)
    skipped = tuple(
        (float(e.fc_hz), e.refusal)
        for e in evaluations if e.refusal == EVAL_REFUSED_BUDGET
    )
    attempted = tuple(
        float(e.fc_hz)
        for e in evaluations if e.refusal != EVAL_REFUSED_BUDGET
    )
    comparison_complete = fc_comparison_complete(evaluations, planned)
    for evaluation in evaluations:
        if evaluation.refusal is not None:
            refusals.append((float(evaluation.fc_hz), evaluation.refusal))
            continue
        scored = score_candidate(evaluation, poses)
        if scored is None:
            refusals.append((float(evaluation.fc_hz), EVAL_REFUSED_NO_BAND))
        else:
            scores.append(scored)
    scores.sort(key=lambda s: (s.score, s.fc_hz))
    ordered = tuple(scores)
    baseline = next(
        (s for s in ordered if math.isclose(s.fc_hz, float(configured_hz))), None
    )
    def _verdict(
        verdict: str, recommended_hz: float | None, margin_db: float | None,
    ) -> FcSelection:
        return FcSelection(
            verdict=verdict, configured_hz=float(configured_hz),
            recommended_hz=recommended_hz, margin_db=margin_db,
            scores=ordered, refusals=tuple(refusals), limits=dict(limits),
            evaluated=len(ordered), planned=int(planned),
            candidate_order=candidate_order, attempted=attempted,
            skipped=skipped, comparison_complete=comparison_complete,
        )

    if baseline is None or len(ordered) < 2:
        # Nothing to compare — the honest verdict is the configured path, and
        # the disclosure's job is to say why nothing was proposed (``limits``
        # carries the derived bounds for exactly that).
        return _verdict(
            SELECTION_NOTHING_TO_COMPARE if baseline is None
            else SELECTION_KEEP_CONFIGURED,
            None, None,
        )
    if not comparison_complete:
        return _verdict(SELECTION_KEEP_CONFIGURED, None, None)
    best = ordered[0]
    improvement = baseline.score - best.score
    if best.fc_hz == baseline.fc_hz or improvement < float(margin_db):
        return _verdict(SELECTION_KEEP_CONFIGURED, None, float(improvement))
    return _verdict(
        SELECTION_RECOMMEND, float(best.fc_hz), float(improvement)
    )
