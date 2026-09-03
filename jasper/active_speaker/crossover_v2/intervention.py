# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The deterministic prescription planner, as pure functions.

:func:`plan_linearization` assembles this repository's existing pure DSP
primitives into one candidate's prescription; no fitter, solver, or estimator
is reimplemented here. Equal inputs give equal outputs — nothing is written,
logged, or mutated, and no clock is read. Journal lines are returned as data
(:class:`JournalRecord`) for the host to emit under its own session identity.

Dependency direction: this module imports the DSP primitives,
:mod:`.contracts` and :mod:`.plan_assembly`. It must never import
:mod:`jasper.active_speaker.crossover_v2_flow` (the flow imports *this*) or
anything under :mod:`jasper.web`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from ..branch_chain import CrossoverSection, radiating_band_hz
from ..branch_target import branch_target
from ..linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    _SIGMA_TOLERABLE_DB as SIGMA_TOLERABLE_DB,
    compose_envelope,
    compute_sigma_curve,
)
from ..linearization_fit import (
    FitVocabulary,
    complex_correction_response,
    core_level_band_hz,
    driver_core_level_db,
    fit_driver_linearization,
    measurement_hole_bands_hz,
)
from ..profile import LEVEL_MATCH_AXIS
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    RealizedLevelMatch,
    realized_branch_level_match,
    ripple_at_trim,
    solve_branch_trims,
    solve_ripple_optimal_trim,
    summed_model_residual_delay_us,
)

from .contracts import (
    CandidateAcousticContext,
    CrossoverV2ContractError,
    TrimStrategy,
)
from .plan_assembly import (
    FittedBranches,
    JournalRecord,
    LevelConsistency,
    LinearizationPlan,
    SummationFrame,
    TrimDecision,
    assemble_plan,
    compose_linearized_prediction,
)

__all__ = [
    "CloudFitTerms",
    "DriverEvidence",
    "LEVEL_DEFINITIONS_DIFFER_REASON",
    "LEVEL_MATCH_AXIS",
    "LEVEL_ESTIMATOR_TOLERANCE_DB",
    "LINEARIZATION_MIN_PAIRED_OCCURRENCES",
    "LINEARIZATION_TRIM_SANITY_MARGIN_DB",
    "LinearizationRequest",
    "MIN_TRIM_SANITY_MARGIN_RATIO",
    "PlannerError",
    "PlannerInputError",
    "SIGMA_TOLERABLE_DB",
    "anchor_trims",
    "compare_level_definitions",
    "compose_sigma_db",
    "decide_trim",
    "driver_response_by_role",
    "measure_validity_floor_hz",
    "plan_linearization",
    "realized_level_match",
    "request_from_analysis",
    "rounded_band_hz",
]


class PlannerError(CrossoverV2ContractError):
    """The planner cannot plan from these inputs — a refusal, not a crash.

    A subclass of :class:`~.contracts.CrossoverV2ContractError` so it inherits
    that base's :attr:`refusal_reason`; the facade maps a raise to a typed
    :class:`~.contracts.PlanRefusal` by reading that attribute, never the
    message text.
    """



class PlannerInputError(PlannerError):
    """A required planner input is missing or malformed."""

    refusal_reason = "contract_invalid"


# What a misbehaving disclosure port may raise without taking the plan down.
# Enumerated rather than a blind ``except Exception`` (ruff BLE). ``OSError`` is
# in the set because the likeliest real consumer is a logging handler, and a
# handler writing to a closed stream or a full disk raises exactly that. A
# consumer raising a CUSTOM exception class escapes the guard and does abort the
# plan; it can raise a stdlib type or wrap.
_PORT_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #

# Minimum paired in-capture occurrences (primary + repeats) per driver before
# the linearization path is trusted at all. A POLICY floor — what linearization
# requires — deliberately not imported from the MEASURE program's own default
# repeat count, which it happens to equal today.
LINEARIZATION_MIN_PAIRED_OCCURRENCES = 3

# dB. How far the ripple-optimal tweeter trim may move from its ANCHORED trim
# (raw trim + that branch's measured level-band give-back, normalized) before
# the scan's result is treated as implausible. Only the SCAN can drift: the
# anchor is measured give-back, not a solver prediction. What the guard catches
# is the scan wandering into the "attenuate the tweeter toward silence is
# always flatter against a flat woofer" degenerate its own +/-window allows.
# Magnitude protection against a garbage correction lives upstream in the fit
# engine's structural caps (per-filter <=12 dB cut, total normalization budget,
# realization tolerance) plus the downstream VERIFY gate, not here.
#
# COUPLED to REALIZED_LEVEL_MATCH_TOLERANCE_DB: the margin must stay at least
# :data:`MIN_TRIM_SANITY_MARGIN_RATIO` times that tolerance, and because the
# margin is a per-request field the relation is checked at construction in
# :meth:`LinearizationRequest.__post_init__`. 6.0 against 2 × 3.0 is exactly on
# that edge.
#
# NOT the same bound as the MEASURE-path ripple polish's, which is coupled to
# that tolerance at 1× and lives in `program_analysis`. This one bounds a
# summed-FLATNESS optimum's drift from a measured LEVEL anchor and wants slack
# over the gate; the polish's bounds a LEVEL excursion the gate then grades
# directly and must not exceed it.
LINEARIZATION_TRIM_SANITY_MARGIN_DB = 6.0

#: The margin must be at least this multiple of
#: :data:`~jasper.audio_measurement.program_analysis.REALIZED_LEVEL_MATCH_TOLERANCE_DB`.
#:
#: Two, derived rather than chosen. The fallback commits the anchor when the
#: scan drifts past the margin ``M``; the accountability seam then grades the
#: committed pair's realized level error against the tolerance ``T``. For that
#: grading to say something about every fallback that leaves the speaker hotter
#: than the scan would have, the drift the fallback can introduce (up to ``M``)
#: has to exceed what the gate tolerates on each side of the crossover (``T``
#: on the anchor's side and ``T`` on the scan's) — i.e. ``M >= 2T``. Below that
#: there is a band of drifts where the anchor is committed, is louder than the
#: scan by more than the gate can see, and the round is SILENT about it.
#:
#: Absolute loudness is not this floor's business: the trims are clamped
#: non-positive and the output limiters and volume rail sit downstream.
MIN_TRIM_SANITY_MARGIN_RATIO = 2.0

# SIGMA_TOLERABLE_DB (per-tier sigma-tolerance table) is imported above from
# linearization_envelope, which owns it -- see the import block near the top
# of this file.


# --------------------------------------------------------------------------- #
# small pure helpers, relocated from the flow so the planner can reach them
# --------------------------------------------------------------------------- #


def driver_response_by_role(analysis: Any, role: str) -> Any | None:
    for resp in analysis.driver_responses:
        if resp.role == role:
            return resp
    return None


def rounded_band_hz(
    band_hz: tuple[float, float] | None,
) -> tuple[float, float | None] | None:
    """A ``(lo, hi)`` band rounded for the journal, with a non-finite upper
    edge rendered as ``None`` rather than ``inf``.

    A high-pass branch radiates to infinity, and ``inf`` is not JSON-safe;
    ``None`` reads as "no upper bound" and survives every consumer. Shared by
    every place that logs a band so they cannot render the same number two
    ways.
    """
    if band_hz is None:
        return None
    lo_hz, hi_hz = band_hz
    return (
        round(float(lo_hz), 1),
        round(float(hi_hz), 1) if math.isfinite(hi_hz) else None,
    )


def measure_validity_floor_hz(analysis: Any) -> float | None:
    """The worse (higher) of the two driver responses' own reflection-gate floor."""

    floors = [
        r.validity_floor_hz
        for r in analysis.driver_responses
        if r.validity_floor_hz is not None
    ]
    return max(floors) if floors else None


def compose_sigma_db(
    own: Any,
    sibling: Any,
    *,
    tier: str,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """The σ-composition policy: the paired-N gate plus the per-tier floor.

    ``own``/``sibling`` are the two
    :class:`~jasper.audio_measurement.program_analysis.DriverResponse` of a
    crossover pair. Returns ``None`` — no evidence, no permission — when EITHER
    driver has fewer than :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES`
    occurrences (primary + repeats); an under-repeated sibling voids the pair's
    trust even if ``own`` alone has plenty. Deliberately redundant with the
    host's own outer eligibility gate, so this function stays independently
    correct when called from a different context. Raises
    :class:`PlannerInputError` for a tier outside the closed set.

    Otherwise computes ``own``'s live σ(f) and floors it at the tier's own
    tolerable value: ``sigma_eff = max(sigma_tolerable(tier), live)``.

    That floor is BEHAVIORALLY INERT at its current value.
    ``repeatability_limit``'s formula is
    ``D_cap * min(1, sigma_tolerable / max(sigma, eps))``, which for any
    ``live <= sigma_tolerable`` already saturates at ``D_cap``. It exists as a
    seam for a policy that sets the floor HIGHER than ``sigma_tolerable``; do
    not assume it currently does more than the paired-N gate above.
    """
    own_n = 1 + len(own.repeat_responses)
    sibling_n = 1 + len(sibling.repeat_responses)
    if (
        own_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
        or sibling_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    ):
        return None
    live = compute_sigma_curve(own, valid_band_hz=valid_band_hz, grid_hz=grid_hz)
    if live is None:
        return None
    try:
        floor_db = SIGMA_TOLERABLE_DB[tier]
    except KeyError as exc:
        # A tier outside the closed set is a caller error, not a measurement
        # outcome — but a bare ``KeyError`` reaching the host is indistinguishable
        # from malformed planner output and would be classified as a generic
        # ``contract_invalid``. Name it instead.
        raise PlannerInputError(
            f"unknown mic tier {tier!r}; expected one of "
            f"{sorted(SIGMA_TOLERABLE_DB)}"
        ) from exc
    return np.maximum(floor_db, live)


def realized_level_match(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    fc_hz: float,
    trims_db: Mapping[str, float],
    woofer_role: str,
    tweeter_role: str,
    *,
    woofer_span_hz: tuple[float, float],
    tweeter_span_hz: tuple[float, float],
) -> RealizedLevelMatch:
    """One candidate trim pair's realized inter-driver level.

    A thin role-ordering adapter over
    :func:`~jasper.audio_measurement.program_analysis.realized_branch_level_match`
    — this layer speaks roles, that estimator speaks woofer/tweeter branches,
    and nothing else belongs in between. A free function so :func:`decide_trim`
    can grade BOTH candidate pairs with one identical call, and so ``fc_hz`` is
    passed rather than read off a session.
    """
    return realized_branch_level_match(
        freqs,
        w_tf,
        t_tf,
        float(fc_hz),
        trim_w_db=float(trims_db[woofer_role]),
        trim_t_db=float(trims_db[tweeter_role]),
        woofer_span_hz=woofer_span_hz,
        tweeter_span_hz=tweeter_span_hz,
    )


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CloudFitTerms:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a
    half-supplied pair, plus the boost-only bound the fit *vocabulary* takes.

    ``boost_excluded_bands_hz`` does NOT go to the envelope. Empty is the
    ordinary case and means "nothing contradicted a boost", never "no
    evidence".
    """

    excluded_bands_hz: tuple[tuple[float, float], ...] = ()
    band_spread: tuple[Any, ...] = ()
    n_positions: int = 0
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_evidence(cls, evidence: Any) -> "CloudFitTerms | None":
        """Adapt the host's cloud-evidence value; ``None`` passes through."""

        if evidence is None:
            return None
        if isinstance(evidence, CloudFitTerms):
            return evidence
        # No ``getattr`` default on any field. A default on
        # ``boost_excluded_bands_hz`` would fail OPEN — a missing field would
        # read as "nothing contradicted a boost" and grant the lift stage the
        # full band, the one direction this bound must never fail in.
        return cls(
            excluded_bands_hz=tuple(
                (float(lo), float(hi)) for lo, hi in evidence.excluded_bands_hz
            ),
            band_spread=tuple(evidence.band_spread),
            n_positions=int(evidence.n_positions),
            boost_excluded_bands_hz=tuple(
                (float(lo), float(hi)) for lo, hi in evidence.boost_excluded_bands_hz
            ),
        )


@dataclass(frozen=True)
class DriverEvidence:
    """One driver's measured response and the two bands that bound its fit.

    ``excited_band_hz`` is the declared sweep span for this role — the band its
    stimulus actually radiated in, which bounds σ-composition and the
    envelope's validity. ``driver_class`` is the envelope's per-class depth
    table key.
    """

    role: str
    response: Any
    excited_band_hz: tuple[float, float]
    driver_class: str = "unknown"


@dataclass(frozen=True)
class LinearizationRequest:
    """Everything :func:`plan_linearization` reads. Nothing else is in scope.

    Holds at most one corner — ``context.fc_hz`` — and nothing that could
    disagree with it.
    """

    context: CandidateAcousticContext | None
    """The one Fc owner: this candidate's corner and the sections realizing it.

    ``None`` on a 1-way main, which declares no crossover; a two-branch request
    without a context is refused below.
    """

    drivers: tuple[DriverEvidence, ...]
    """The branches to fit, lowest first. One (a 1-way main) or two."""

    raw_trim_db: Mapping[str, float]
    """``candidate.trim_db`` — the raw solve's applied per-role attenuation.

    Empty on a 1-way main: the trim solve places a PAIR.
    """

    trim_band_average_db: Mapping[str, float]
    """``candidate.trim_band_average_db`` — one SUBORDINATE level estimate.

    The trim solve's own per-role level-match term, a power-band average each
    side of Fc taken BEFORE the ripple-optimal polish moves it. It does not
    place the pair: it is an input to :func:`compare_level_definitions` and
    nothing else, and it is not preferred over the fit's own core-level
    estimate.
    """

    predicted_ripple_db: float
    polarity_sign: int
    delay_us: float
    anchor_delay_us: float | None
    """``None`` when the alignment is not :data:`ALIGNMENT_OK` — load-bearing."""

    mic_tier: str
    branch_floor_hz: float | None
    """The shared reflection-gate floor; ``None`` when no response reported one."""

    post_apply_verifies: bool
    """Does the JOURNEY measure what the speaker did with a boost? Boost's ONE
    necessary condition."""

    cloud_phase_planned: bool
    """Did this session PLAN a spatial cloud? Distinguishes "absent by design"
    (the driver-only path, boost permitted) from "planned and lost" (boost
    withheld) — the two share a ``cloud is None`` signature and are different
    evidence states."""

    cloud: CloudFitTerms | None = None
    trim_sanity_margin_db: float = LINEARIZATION_TRIM_SANITY_MARGIN_DB

    def __post_init__(self) -> None:
        drivers = tuple(self.drivers)
        if not 1 <= len(drivers) <= 2:
            raise PlannerInputError("a request carries one or two drivers")
        if self.context is None and len(drivers) != 1:
            raise PlannerInputError("a two-branch request needs a context")
        if self.context is not None and not isinstance(
            self.context, CandidateAcousticContext
        ):
            raise PlannerInputError("context must be a CandidateAcousticContext")
        for driver in drivers:
            if not isinstance(driver, DriverEvidence):
                raise PlannerInputError("drivers must be DriverEvidence values")
            if driver.response is None:
                raise PlannerInputError(
                    f"{driver.role!r} evidence carries no response"
                )
        if len({driver.role for driver in drivers}) != len(drivers):
            raise PlannerInputError(
                f"two branches share the role {drivers[0].role!r}"
            )
        object.__setattr__(self, "drivers", drivers)
        if int(self.polarity_sign) not in (-1, 1):
            raise PlannerInputError("polarity_sign must be -1 or +1")
        # The hearing-safety coupling, checked rather than trusted. NaN is
        # tested first and separately: every comparison against NaN is False,
        # so a NaN margin does not widen the guard, it deletes it.
        margin = float(self.trim_sanity_margin_db)
        if not math.isfinite(margin):
            raise PlannerInputError(
                "trim_sanity_margin_db must be finite; a non-finite margin "
                "silently disables the sanity guard rather than widening it"
            )
        floor = MIN_TRIM_SANITY_MARGIN_RATIO * REALIZED_LEVEL_MATCH_TOLERANCE_DB
        if margin < floor:
            raise PlannerInputError(
                f"trim_sanity_margin_db {margin} dB is below "
                f"{MIN_TRIM_SANITY_MARGIN_RATIO}x the realized-level tolerance "
                f"({REALIZED_LEVEL_MATCH_TOLERANCE_DB} dB): the anchor fallback "
                "could then commit a pair louder than the scan by more than the "
                "accountability gate can detect"
            )
        # A non-finite trim would reach :func:`anchor_trims` and be COMMITTED:
        # the non-positive normalize is a ``max``, and every comparison against
        # NaN is False, so the clamp stops existing rather than misfiring and a
        # NaN or +inf trim goes to the emitter. This and the give-back check at
        # the anchor's own call site are the complete set of doors into it.
        for name, mapping in (
            ("raw_trim_db", self.raw_trim_db),
            ("trim_band_average_db", self.trim_band_average_db),
        ):
            for role, value in mapping.items():
                if not math.isfinite(float(value)):
                    raise PlannerInputError(
                        f"{name}[{role!r}] is not finite; a non-finite trim "
                        "reaches the emitter because the non-positive "
                        "normalize compares against NaN and does nothing"
                    )
        # Snapshot the caller's trim mappings: a frozen dataclass holding a live
        # dict is frozen in name only, and these are read at several points
        # spread across the plan.
        object.__setattr__(self, "raw_trim_db", dict(self.raw_trim_db))
        object.__setattr__(
            self, "trim_band_average_db", dict(self.trim_band_average_db)
        )

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(driver.role for driver in self.drivers)

    def sections_for(self, role: str) -> tuple[CrossoverSection, ...]:
        """This candidate's sections for ``role`` — empty when it has none.

        Empty is *representable* and no section is invented: the emitter builds
        its crossover filters from the same regions, so such a role runs full
        range in the emitted graph. Representable is not correct — on a 2-way
        session it is a defect upstream, which is why
        :func:`plan_linearization` names it in the journal.

        The *context* separately guarantees that every section which does exist
        names this candidate's corner. A 1-way main carries no context at all.
        """
        if self.context is None:
            return ()
        return self.context.sections_by_role.get(role, ())


# --------------------------------------------------------------------------- #
# the level datum's one owner, its subordinate checks, and the anchor it places
# --------------------------------------------------------------------------- #

#: Why a record was banked: the two level DEFINITIONS differ by more than the
#: disclosure trigger.
#:
#: *level_definitions*: the HANDOVER level
#: (:func:`~jasper.audio_measurement.program_analysis.solve_branch_trims`, a
#: power-band average over the mirrored ±1-octave halves about Fc) and the
#: PASSBAND estimate
#: (:func:`~jasper.active_speaker.linearization_fit.driver_core_level_db`, a
#: median over the driver's own core/radiating band). *differ*: their relative
#: placements of the pair sit further apart than
#: :data:`LEVEL_ESTIMATOR_TOLERANCE_DB`.
#:
#: They never measured one quantity, so a gap is not a disagreement: different
#: bands, different statistics, and on a horn with a sloped response the two
#: conventions legitimately part company by many dB. The handover level is THE
#: level fact; the passband estimate sizes the fixed attenuation. The gap is
#: reported, never reconciled.
LEVEL_DEFINITIONS_DIFFER_REASON = "level_definitions_differ"

#: How far the two level definitions may sit apart before the difference is
#: DISCLOSED.
#:
#: **A disclosure trigger, not an agreement bar.** Nothing here decides whether
#: a number is right; it decides when the gap is worth putting in front of
#: someone. The same 3.0 dB the realized-level gate holds a shipped pair to
#: (``REALIZED_LEVEL_MATCH_TOLERANCE_DB``), reused so both answer "how far apart
#: may two reads of the same inter-driver relationship sit" with one number.
#:
#: It must never be narrowed to the recipe's OTHER tolerance. That ≤0.5 dB is
#: the SETTING precision — how close the trim must land once the level fact is
#: known — and is already met by construction, since the trim search steps at
#: ``RIPPLE_TRIM_SEARCH_STEP_DB`` (0.1 dB), five times finer.
#:
#: ``baseline_profile._compare_level_sittings`` is not a duplicate of this one:
#: it compares two SITTINGS of the single handover definition, where this
#: compares two definitions on one capture.
LEVEL_ESTIMATOR_TOLERANCE_DB = REALIZED_LEVEL_MATCH_TOLERANCE_DB

#: Why a capture was flagged: the COMMITTED pair's two realized levels sit
#: further apart than :data:`REALIZED_LEVEL_MATCH_TOLERANCE_DB`.
#:
#: The sibling of :data:`LEVEL_DEFINITIONS_DIFFER_REASON`. *realized_levels*:
#: not two estimates of where the drivers sit, but where the pair that would
#: ship actually lands. *disagree*: past the tolerance above.
#:
#: It flags a capture; it never refuses one
#: (`docs/measurement-loop-doctrine.md` deviation (i)).
REALIZED_LEVEL_SUSPECT_REASON = "realized_levels_disagree"


def _relative_placement_db(levels: Mapping[str, float]) -> dict[str, float]:
    """One estimator's placement of the pair, referred to its own loudest role.

    Absolute levels from different instruments are not comparable — a
    power-band average about Fc and a core-band median each carry their own
    reference. What IS comparable is how far apart an estimator puts the two
    roles. Subtracting the loudest role makes every estimator state that same
    relative claim, so ``|a − b|`` per role is a difference of PLACEMENTS
    rather than a difference of references.
    """

    if not levels:
        return {}
    loudest = max(float(v) for v in levels.values())
    return {role: float(value) - loudest for role, value in levels.items()}


def compare_level_definitions(
    *,
    trim_band_average_db: Mapping[str, float],
    core_proposal_db: Mapping[str, float],
    tolerance_db: float = LEVEL_ESTIMATOR_TOLERANCE_DB,
) -> LevelConsistency | None:
    """How far apart do the handover level and the passband estimate sit?

    **It reports a difference. It does not ask whether they agree** — the two
    numbers never measured one quantity:

    * ``trim_band_average_db`` is the HANDOVER level —
      :func:`~jasper.audio_measurement.program_analysis.solve_branch_trims`'
      power-band average over the mirrored ±1-octave halves about Fc. After the
      target filters, the two traces are equal at Fc and each sits −6 dB against
      the summed target; that Linkwitz-Riley unity condition makes it **the**
      level fact.
    * ``core_proposal_db`` is the PASSBAND estimate — the fit's core-band median
      (:func:`~jasper.active_speaker.linearization_fit.driver_core_level_db`)
      expressed as the system-referred level each role's own passband proposes
      (``core_level + trim``). Subordinate: the starting estimate that sizes the
      horn's fixed attenuation.

    Different bands, different statistics. **On a horn with a sloped response
    they legitimately differ by many dB**, so the gap is surfaced with its
    number and never reconciled. Neither number places anything either way: the
    pair is anchored on the raw measured trim (:func:`anchor_trims`).

    Compared in a RELATIVE frame — each definition referred to its own loudest
    role — because the two carry different references and only their PLACEMENT
    of the pair is comparable.

    Returns ``None`` when either definition covers no role.
    """

    if not trim_band_average_db or not core_proposal_db:
        return None
    trim_rel = _relative_placement_db(trim_band_average_db)
    core_rel = _relative_placement_db(core_proposal_db)
    # Only roles BOTH definitions cover. A role one of them skipped is not a
    # difference.
    delta = {
        role: abs(trim_rel[role] - core_rel[role])
        for role in core_rel
        if role in trim_rel
    }
    worst = max(delta.values(), default=0.0)
    differs = worst > float(tolerance_db)
    return LevelConsistency(
        differs=differs,
        reason=LEVEL_DEFINITIONS_DIFFER_REASON if differs else "",
        tolerance_db=float(tolerance_db),
        worst_delta_db=float(worst),
        estimator_delta_db=delta,
        matched_axis=LEVEL_MATCH_AXIS,
    )


def anchor_trims(
    *,
    roles: tuple[str, ...],
    anchor_base_db: Mapping[str, float],
    giveback_db: Mapping[str, float],
) -> tuple[dict[str, float], float]:
    """Place the anchored trim pair and normalize it non-positive.

    Returns ``(anchored_db, normalize_shift_db)``. The anchor is
    ``base + giveback``: no third term, no branch, no threshold.
    ``anchor_base_db`` is the raw measured trim — the number the branch solve
    measured — so there is nothing to arbitrate here. The two per-driver level
    estimators are compared by :func:`compare_level_definitions`, which banks a
    finding and moves no number.

    **The non-positive normalize is the hearing-safety invariant here.** Every
    returned trim is ``<= 0``: a branch whose own cuts give back more than its
    raw attenuation would otherwise land POSITIVE (a boost), which the emitter
    refuses and the hardware must never see. The shift is subtracted from every
    role identically, so it preserves relative leveling exactly and is honest
    extra ledger rather than a tonal change.
    """

    unnormalized = {
        role: float(anchor_base_db.get(role, 0.0))
        + float(giveback_db.get(role, 0.0))
        for role in roles
    }
    shift = max(0.0, max(unnormalized.values()))
    return {r: v - shift for r, v in unnormalized.items()}, shift



# --------------------------------------------------------------------------- #
# the trim decision
# --------------------------------------------------------------------------- #


def decide_trim(
    *,
    anchored_db: Mapping[str, float],
    resolved_db: Mapping[str, float],
    tweeter_role: str,
    anchored_match: RealizedLevelMatch,
    resolved_match: RealizedLevelMatch,
    ripple_db: float | None,
    sanity_margin_db: float = LINEARIZATION_TRIM_SANITY_MARGIN_DB,
) -> TrimDecision:
    """Commit one trim pair, and say which one and why.

    Two regimes, and the boundary between them is ``sanity_margin_db``. Within
    it the scan is trusted to have polished, so BOTH pairs are graded by their
    realized inter-driver level and the better-levelled one is committed; ties
    go to the anchor, which is level-preserving by construction. Beyond it the
    scan is not trusted at all and the anchor is committed —
    :attr:`~.contracts.TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT`.
    A beyond-margin scan that genuinely levelled better is still refused.

    The fallback RAISES the committed level, because the anchor is the *less*
    attenuated pair whenever the scan drifted downward (the degenerate
    direction the margin exists to catch). It is bounded on both sides: the
    anchor is level-*preserving* — each branch's own measured give-back,
    normalized so no trim is ever positive — so the committed pair can never
    exceed the branch's own pre-correction system level and the emitted trims
    stay cut-only; and the committed pair is still MEASURED afterwards at the
    host's accountability seam, which discloses rather than refuses
    (`docs/measurement-loop-doctrine.md` deviation (i)), so a badly-levelled
    anchor produces a round that SAYS so.

    That second bound is CONDITIONAL. It holds only while

        ``sanity_margin_db >= MIN_TRIM_SANITY_MARGIN_RATIO *
        REALIZED_LEVEL_MATCH_TOLERANCE_DB``

    — today 6.0 against 2 × 3.0, exactly on the edge. Below it there is a band
    of drifts where the anchor is committed, is louder than the scan by more
    than the accountability check can SEE, and so ships with the round saying
    nothing about it. Because this function takes the margin as an argument,
    the relation is validated in :meth:`LinearizationRequest.__post_init__`,
    where the margin enters.
    """
    drift_db = abs(
        float(resolved_db[tweeter_role]) - float(anchored_db[tweeter_role])
    )
    # EXCLUSIVE, and tolerant at the boundary itself. ``drift_db`` is a
    # difference of two doubles neither of which is exactly representable, so a
    # drift that IS the margin can land a ULP either side of it depending on the
    # last bits of the anchor, which come out of numpy reductions whose SIMD
    # path varies by build — a bare ``>`` would make "exactly at the margin" an
    # interpreter-dependent coin flip.
    #
    # The tolerance is 1e-9 RELATIVE — nine orders of magnitude below the
    # smallest drift anyone can hear, and far above the ~1e-15 the arithmetic
    # can produce — so it separates "the same number" from "a different number"
    # and nothing else. It errs toward TRUSTING a scan that landed on the
    # margin.
    beyond = drift_db > float(sanity_margin_db) and not math.isclose(
        drift_db, float(sanity_margin_db), rel_tol=1e-9, abs_tol=0.0
    )
    anchor_levels_better = abs(anchored_match.difference_db) <= abs(
        resolved_match.difference_db
    )
    if beyond:
        committed_db, committed_match = anchored_db, anchored_match
        strategy = TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
        rationale = (
            f"the ripple scan drifted {drift_db:.3f} dB from the anchor, past "
            f"the {float(sanity_margin_db):.1f} dB sanity margin, so it was "
            "rejected and the level-preserving anchored pair was committed."
        )
    elif anchor_levels_better:
        committed_db, committed_match = anchored_db, anchored_match
        strategy = TrimStrategy.ANCHORED_COMMITTED
        rationale = (
            "the anchored pair realized the better inter-driver level "
            f"({anchored_match.difference_db:+.3f} dB against the scan's "
            f"{resolved_match.difference_db:+.3f} dB); the scan stayed within "
            "the sanity margin."
        )
    else:
        committed_db, committed_match = resolved_db, resolved_match
        strategy = TrimStrategy.RESOLVED_COMMITTED
        rationale = (
            "the ripple scan's pair realized the better inter-driver level "
            f"({resolved_match.difference_db:+.3f} dB against the anchor's "
            f"{anchored_match.difference_db:+.3f} dB) and stayed within the "
            "sanity margin."
        )
    return TrimDecision(
        committed_db=dict(committed_db),
        strategy=strategy,
        rationale=rationale,
        anchored_db=dict(anchored_db),
        resolved_db=dict(resolved_db),
        anchor_drift_db=drift_db,
        sanity_margin_db=float(sanity_margin_db),
        beyond_sanity_margin=beyond,
        committed_match=committed_match,
        ripple_db=None if ripple_db is None else float(ripple_db),
    )


def plan_linearization(
    request: LinearizationRequest,
    *,
    journal: Callable[[JournalRecord], None] | None = None,
) -> LinearizationPlan:
    """Fit every branch, correct in the linear domain, commit one trim pair.

    The ordering is load-bearing: fit each branch, apply the correction as a
    COMPLEX (minimum-phase) response, then re-solve the trim from the
    LINEARIZED branch pair, which is what structurally defuses the
    band-average trim bias.

    On a 1-way main there is no corner, no overlap and no pair to grade: the
    branch is fitted as either branch of a pair is, ships at a fixed 0 dB, and
    the plan carries no trim decision.

    Assumes eligibility (reference-tier mic, both drivers paired
    N ≥ :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES`) and does not re-check —
    the host's own gate owns that question, and a request that fails it will
    raise inside the fit engine rather than quietly returning a worse plan.
    Its branch inputs must already carry the crossover shoulders.

    ``journal`` is an optional write-only disclosure port: every record is
    handed to it as it is produced, and every record is also on the returned
    plan, so a host that passes one sees the lines a raising fit had reached. A
    port that raises is contained — the record still reaches the plan and the
    refusal is listed in :attr:`LinearizationPlan.journal_dropped`.
    """
    roles = request.roles
    context = request.context
    fc_hz = None if context is None else context.fc_hz
    responses = {driver.role: driver.response for driver in request.drivers}
    # The σ gate reads each branch's REPEAT count against its sibling's; a lone
    # branch is its OWN sibling, reducing the paired-N gate to its own count.
    siblings = {
        role: responses[next((other for other in roles if other != role), role)]
        for role in roles
    }
    excited_band_hz = {
        driver.role: driver.excited_band_hz for driver in request.drivers
    }
    driver_class = {driver.role: driver.driver_class for driver in request.drivers}
    records: list[JournalRecord] = []
    dropped: list[str] = []

    def emit(event: str, fields: Mapping[str, Any], level: int = logging.INFO) -> None:
        record = JournalRecord(event, fields, level)
        records.append(record)
        if journal is None:
            return
        try:
            journal(record)
        except _PORT_ERRORS as exc:
            # A disclosure port that can abort the plan is worse than no port:
            # a host formatter that throws on one field would otherwise cost a
            # household the whole round. The record is still on the returned
            # plan, and the failure is disclosed on ``journal_dropped``.
            dropped.append(f"{record.event}: {type(exc).__name__}: {exc}")

    # --- each branch's own crossover, and the band it radiates in ----------
    #
    # The fit's LIFT stage is bounded to the radiating band: a driver measured
    # THROUGH its crossover carries that crossover's rolloff in its curve, and a
    # fit flattening it against a flat target reads the rolloff as a driver
    # deficit and boosts it back — including at the knee, where a branch is 6 dB
    # down BY DESIGN.
    #
    # LIFT is bounded at the band ITSELF; the SOLVE, cuts included, is bounded
    # at the band WIDENED by half an octave
    # (``linearization_fit._solve_band_mask``), which is a looser bound and not
    # this one. The asymmetry is deliberate: leakage past the handoff still
    # reaches the summed response and removing it spends no headroom, so a
    # shoulder cut is kept.
    #
    # The band ALSO bounds one LEVEL question, and only one: the core-level
    # median below. It stops at the median — the give-back is a power-domain
    # average that quiet stopband bins barely reach.
    #
    # These sections are the CANDIDATE's, from the request's context, and every
    # one of them names ``fc_hz``: that is checked at construction, not here.
    # Each Fc candidate must be fitted against ITS OWN crossover, or the
    # comparison measures the fit's mismatch instead of the crossover's.
    sections = {role: request.sections_for(role) for role in roles}
    rounded_fc_hz = None if fc_hz is None else round(float(fc_hz), 3)
    # The named defect, disclosed at the site that detects it. See
    # :meth:`LinearizationRequest.sections_for` for why the condition is
    # representable and still a defect — but only where a crossover was
    # declared at all: a 1-way main's branch runs full range BY DESIGN.
    if context is not None:
        for role in roles:
            if not sections[role]:
                emit(
                    "correction.crossover_v2_linearization_no_crossover",
                    {"role": role, "fc_hz": rounded_fc_hz},
                    logging.WARNING,
                )
    radiating_bands = {role: radiating_band_hz(sections[role]) for role in roles}
    # At the CANDIDATE's corner, like every corner-driven call below.
    emit(
        "correction.crossover_v2_linearization_fit_band",
        {
            "fc_hz": rounded_fc_hz,
            "radiating_band_hz": {
                role: rounded_band_hz(band) for role, band in radiating_bands.items()
            },
            "crossover_order": {
                role: tuple(s.order for s in sections[role]) for role in roles
            },
        },
    )

    # --- the session's ONE shared level frame ------------------------------
    #
    # Every envelope is composed FIRST, so each driver's own core-passband level
    # is read off it before any driver is fitted.
    envelopes: dict[str, Any] = {}
    for role in roles:
        resp = responses[role]
        sigma_db = compose_sigma_db(
            resp,
            siblings[role],
            tier=request.mic_tier,
            valid_band_hz=excited_band_hz[role],
        )
        # The cloud seam. All three arguments are ``None`` when no cloud verdict
        # was available. They can only ever NARROW allowed depth: they enter the
        # same ``np.min`` as every other term, so no cloud can buy the fit
        # permission it did not already have.
        cloud = request.cloud
        envelopes[role] = compose_envelope(
            role,
            resp,
            excited_band_hz=excited_band_hz[role],
            mic_tier=request.mic_tier,
            driver_class=driver_class[role],
            sigma_db=sigma_db,
            excluded_bands_hz=cloud.excluded_bands_hz if cloud else None,
            band_spread=cloud.band_spread if cloud else None,
            n_positions=cloud.n_positions if cloud else None,
        )
    core_levels_db = {
        role: level
        for role in roles
        if (
            level := driver_core_level_db(
                responses[role],
                envelopes[role],
                radiating_band_hz=radiating_bands[role],
            )
        )
        is not None
    }
    # The trim solve's own level-match result (``trim_band_average_db``) — NOT
    # ``trim_db``, which is that result AFTER the ripple-optimal polish moved it
    # for summed flatness. The polish is a flatness refinement and this check is
    # about level, so reading the applied trim would make the check sensitive to
    # a refinement it is not measuring. Falls back to ``trim_db`` only for a
    # candidate constructed before the field existed. A CHECK INPUT, not a
    # voter: nothing derived from it places the pair.
    trim_band_estimate_db = dict(request.trim_band_average_db or request.raw_trim_db)
    # The other subordinate estimator, in the frame the check compares in: the
    # system-referred level each role's own passband proposes.
    core_proposal_db = {
        role: float(level) + float(trim_band_estimate_db.get(role, 0.0))
        for role, level in core_levels_db.items()
    }
    # The two DEFINITIONS against each other. Symmetric, disclosive, and unable
    # to move a number — see :func:`compare_level_definitions`. Crossing the
    # trigger discloses the gap; the round proceeds on the raw measured trim.
    level_consistency = compare_level_definitions(
        trim_band_average_db=trim_band_estimate_db,
        core_proposal_db=core_proposal_db,
    )
    # The frame's own INPUTS, for the finding's journal line, so a reader does
    # not have to re-derive which driver read what and over which band.
    #
    # BOTH bands are reported. ``radiating_band_hz`` is the bound asked for;
    # ``band_hz`` is the span the median was actually taken over. The
    # interesting divergence is the width floor REFUSING the bound for leaving
    # too little band. The two also differ by a grid snap in the ORDINARY case,
    # always — ``band_hz`` is resolved onto the envelope's own bins, so its
    # edges are the outermost bins inside the declared span — so equality is the
    # exception, and only inequality of MORE than a bin means the floor fired.
    #
    # ONE read per role, shared by the journal line below and the
    # measurement-hole derivation after it, so the band a filter is named
    # against and the band a reader is shown cannot drift.
    core_bands_hz = {
        role: core_level_band_hz(
            envelopes[role], radiating_band_hz=radiating_bands[role]
        )
        for role in roles
    }
    core_level_evidence = {
        role: {
            "level_db": round(float(level), 3),
            "band_hz": rounded_band_hz(core_bands_hz[role]),
            "radiating_band_hz": rounded_band_hz(radiating_bands[role]),
        }
        for role, level in core_levels_db.items()
    }
    # The per-branch MEASUREMENT HOLE. Each branch's core band stops where its
    # own crossover hands off, so between the woofer's top and the tweeter's
    # bottom there is a span NEITHER capture covers while the summed response
    # there is a live two-branch blend. A hole is a property of the PAIR, so it
    # is derived here — the composer is the only layer holding both roles — and
    # handed to each fit as a band, keeping ``fit_driver_linearization`` a
    # single-branch function that is told things rather than one that knows
    # about crossovers. Full precision, not the journal's rounded copy.
    blind_bands_hz = measurement_hole_bands_hz(
        [core_bands_hz[role] for role in roles]
    )

    # The overlap-band estimator, for the banked finding. Restricted to the
    # roles the core median was actually read over, so the banked pair is the
    # pair that was compared rather than every role the trim solve returned.
    banked_trim_estimate_db = {
        role: float(trim_band_estimate_db[role])
        for role in core_levels_db
        if role in trim_band_estimate_db
    }

    # Boost permission is EVIDENCE-gated, and this is the gate. ONE necessary
    # condition, plus a clause that distinguishes two ways a cloud can be
    # missing.
    #
    # 1. NECESSARY: the JOURNEY will MEASURE what the speaker did with the boost
    #    (``post_apply_verifies``). Every plan shape declares a post-apply check
    #    today, so this reads as always-on — but stating the condition rather
    #    than a constant means a future tier that drops the post-apply sweep
    #    drops boost with it instead of shipping an unverified one.
    #
    # 2. The cloud clause. On the driver-only path there is no cloud to wait
    #    for, and boost is permitted there by owner ruling, on the named and
    #    accepted risk that a boost can land on a position-specific artifact an
    #    at-mark verification cannot detect — adjudicated by post-apply VERIFY,
    #    household listening, and retained Undo. See the "Boost ruling" block in
    #    ``docs/historical/linearization-campaign-2026-07.md`` §4.2.
    #
    #    A session that PLANNED a cloud and LOST it is the other case, and it
    #    withholds boost: ``compose_envelope`` then receives
    #    ``excluded_bands_hz=None``, so ``allowed_depth_db`` is NOT zeroed in
    #    the registry's interference nulls, and granting boost would let the fit
    #    EQ a null the session's own instrument was supposed to have found.
    #    Absent by DESIGN and absent by FAILURE share the ``cloud is None``
    #    signature; ``cloud_phase_planned`` is what tells them apart.
    #
    # What bounds a boost once permitted, on EITHER path: the envelope's own
    # depth limits, the realized-cascade stopband-gain guard, the measured-target
    # bound (no boost where the MEASUREMENT is already at or above target,
    # however the post-cut working curve reads), the headroom charge, post-apply
    # VERIFY, and Undo. The gate grants a vocabulary, never a filter.
    vocabulary = FitVocabulary(
        allow_boost=request.post_apply_verifies
        and (request.cloud is not None or not request.cloud_phase_planned),
        boost_excluded_bands_hz=(
            request.cloud.boost_excluded_bands_hz if request.cloud is not None else ()
        ),
    )

    fits: dict[str, Any] = {}
    corrections: dict[str, np.ndarray] = {}
    for role in roles:
        resp = responses[role]
        fit = fit_driver_linearization(
            resp,
            envelopes[role],
            vocabulary=vocabulary,
            radiating_band_hz=radiating_bands[role],
            # The spans no branch measured. Same list for both roles — a hole
            # belongs to the pair, not to whichever branch happens to be
            # fitting when it is reached.
            blind_bands_hz=blind_bands_hz,
            # The branch's own committed crossover as the fit's target SHAPE,
            # built from the SAME ``sections`` the radiating band above and the
            # emitter's own filters come from, so the shape the fit aims at and
            # the filter the graph runs cannot disagree. ``None`` for a role
            # with no committed region, which is a flat target exactly.
            target=branch_target(sections[role], envelopes[role].freqs_hz),
        )
        fits[role] = fit
        # The cloud bound's per-filter verdicts, disclosed as a journal record
        # rather than from inside the fit: ``linearization_fit`` is pure
        # computation and owns no logger. Emitted only when the bound actually
        # decided something.
        if fit.lift_boost_excluded_drops or fit.lift_boost_excluded_residual:
            emit(
                "correction.crossover_v2_boost_excluded_verdicts",
                {
                    "role": role,
                    # Dropped: the boost was AIMED at a contradicted band.
                    "dropped": [d.to_dict() for d in fit.lift_boost_excluded_drops],
                    # Kept: skirt tail from filters working elsewhere. Accepted
                    # and measured by the post-apply sweep, not refused here.
                    "residual": [r.to_dict() for r in fit.lift_boost_excluded_residual],
                    "lift_suppressed_reason": fit.lift_suppressed_reason,
                },
                logging.WARNING if fit.lift_boost_excluded_drops else logging.INFO,
            )
        # The measured-target verdicts, disclosed the same way. Two events
        # rather than one because they are different verbs and a reader
        # filtering the journal wants them apart: the first REFUSED something,
        # the second let something ship and named it.
        if fit.lift_boost_evidence_drops:
            emit(
                "correction.crossover_v2_boost_measured_target_verdicts",
                {
                    "role": role,
                    # Each boost refused because its whole action region sat
                    # where the MEASUREMENT is already at or above target.
                    "dropped": [d.to_dict() for d in fit.lift_boost_evidence_drops],
                    # Set only when EVERY boost was refused; a partial refusal
                    # leaves it empty because a lift still happened.
                    "lift_suppressed_reason": fit.lift_suppressed_reason,
                },
                # Always WARNING: unlike the block above there is no
                # accepted-remainder case here, so a record exists only when a
                # boost was refused.
                logging.WARNING,
            )
        if fit.blind_zone_placements:
            emit(
                "correction.crossover_v2_blind_zone_placements",
                {
                    "role": role,
                    # Every EMITTED Peaking filter — cut or lift boost — whose
                    # centre landed where no branch's own capture reaches.
                    "placed": [p.to_dict() for p in fit.blind_zone_placements],
                    # The session's holes themselves, so the record is
                    # self-contained rather than needing the level-frame line
                    # beside it to be interpretable.
                    "blind_bands_hz": [list(band) for band in blind_bands_hz],
                },
                # WARNING when a filter ADDS level into a blend nothing
                # measured, INFO when they only remove it. Both ship either
                # way; this is the severity of the disclosure, not a gate.
                logging.WARNING if any(
                    p.gain_db > 0.0 for p in fit.blind_zone_placements
                ) else logging.INFO,
            )
        # COMPLEX (minimum-phase) correction, not a zero-phase magnitude scale
        # — see :func:`compose_linearized_prediction` for the measured cost of
        # the magnitude-only model. The single seam: these corrected branches
        # feed all three consumers (the trim re-solve, the ripple-optimal scan,
        # and the persisted VERIFY prediction).
        corrections[role] = complex_correction_response(fit.filters, resp.freqs_hz)

    fitted = FittedBranches(
        fc_hz=fc_hz,
        fits=fits,
        sections=sections,
        radiating_band_hz=radiating_bands,
        core_level_evidence=core_level_evidence,
        trim_band_estimate_db=banked_trim_estimate_db,
        level_consistency=level_consistency,
    )

    if len(roles) == 1:
        # One branch, so no handoff to level: it ships at a fixed 0 dB.
        role = roles[0]
        attenuations = {role: 0.0}
        frame = SummationFrame(
            freqs_hz=responses[role].freqs_hz,
            branch_tf={role: responses[role].complex_tf},
            polarity_sign=1,
            residual_delay_us=0.0,
        )
        return assemble_plan(
            fitted,
            role_attenuations_db=attenuations,
            trim=None,
            polish_delta_db={},
            summation_frame=frame,
            linearized_predicted_sum=compose_linearized_prediction(
                frame,
                filters_by_role={
                    role: [f.to_dict() for f in fits[role].filters]
                },
                role_attenuations_db=attenuations,
            ),
            emit=emit,
            records=records,
            dropped=dropped,
        )

    woofer_role, tweeter_role = roles
    # A narrowing, not a check: a two-branch request carries a context.
    assert fc_hz is not None

    freqs = responses[woofer_role].freqs_hz
    w_lin = responses[woofer_role].complex_tf * corrections[woofer_role]
    t_lin = responses[tweeter_role].complex_tf * corrections[tweeter_role]

    # Same gating-consistent overlap band the raw trim solve used, so the
    # comparison below is apples to apples: same band, linearized vs raw branch
    # content. At the CANDIDATE's corner.
    lo, hi = overlap_band_hz(
        fc_hz,
        tweeter_sweep_lo_hz=excited_band_hz[tweeter_role][0],
        woofer_sweep_hi_hz=excited_band_hz[woofer_role][1],
    )
    branch_floor_hz = request.branch_floor_hz
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )

    # Each branch's OWN excited-and-gated validity span, the shape
    # ``branch_level_bands_hz`` takes — the declared sweep band, floored by the
    # shared reflection floor — so the realized-level check reads the same frame
    # the raw trim solve read, one layer up on the linearized branches.
    def _span(role: str) -> tuple[float, float]:
        span_lo, span_hi = excited_band_hz[role]
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
            span_lo = max(span_lo, branch_floor_hz)
        return float(span_lo), float(span_hi)

    woofer_span = _span(woofer_role)
    tweeter_span = _span(tweeter_role)

    # ANCHORED give-back — measured in the SAME FRAME IT IS SPENT IN.
    #
    # **THE INVARIANT.** A give-back that adjusts a trim must be measured with
    # the same estimator, in the same averaging domain, and over the SAME BANDS
    # as the trim it adjusts and the verdict that grades that trim. Here that
    # estimator is :func:`solve_branch_trims` over ``branch_level_bands_hz``,
    # which is what :func:`realized_level_match` re-reads to grade the committed
    # pair. Any other band answers a different question, and the difference
    # lands as inter-driver level error. A core-band give-back against a
    # crossover-halves verdict barely overlaps on a compression horn, and the
    # per-role difference passed straight through as up to 3.67 dB of it.
    #
    # **The invariant has a PRECONDITION, and it is BOUNDED.** The give-back is
    # the right adjustment for a base that came from this same solve.
    # ``raw_trim_db`` usually did. But ``program_analysis``' MEASURE path may
    # hand over the RIPPLE-POLISHED tweeter trim instead — a FLATNESS choice —
    # and the base is then δ away from what this give-back is calibrated to,
    # with δ passing straight through as realized inter-driver level error.
    # Precisely, that error is the two roles' δ DIFFERENCE (a shift both share
    # is common mode and normalizes away), reducing to the tweeter's δ alone
    # only because the candidate build ripple-solves ``trim_t`` and commits the
    # woofer's band-average seed unchanged. `program_analysis` admits the polish
    # only within ``REALIZED_LEVEL_MATCH_TOLERANCE_DB`` and otherwise falls back
    # to the band-average seed, so the bound is |δ| ≤ that tolerance
    # (`docs/measurement-loop-doctrine.md` deviation (i)). The delta itself is
    # published every round as ``polish_delta_db`` and carried onto the plan.
    #
    # **This LEVEL-MATCHES; it does not quieten. Read that before assuming a
    # safety direction.** The committed trim moves by exactly the realized level
    # error the anchor was carrying, in whichever direction that error sat, so a
    # branch whose correction lives INSIDE the graded band legitimately ends up
    # HOTTER — still under the non-positive clamps below, and still
    # level-correct. What is restored is equality between the two branches at
    # the handoff, not a monotone reduction in level. The rails here are the
    # CLAMPS — the non-positive trim clamps below, the output limiters,
    # `devices.volume_limit`, and the commissioning SPL stop — NOT the
    # realized-level check, which never bounded absolute output and now
    # discloses rather than stopping anything.
    #
    # The estimator's own bias cannot reach the verdict, and the reason is
    # stronger than "it cancels": it TELESCOPES out entirely. The verdict is
    # ``(level_t_pre - level_w_pre) + (raw_t - raw_w)``, every term of which
    # this same call produces, so ``solve_branch_trims``' known +0.54 dB
    # linear-grid systematic enters with one sign and leaves nothing behind.
    # Cancellation would be the weaker claim AND a false one — the per-role
    # biases differ by ~0.45 dB. A cross-band route has neither property.
    band_average_trim_w_db, band_average_trim_t_db, level_w_pre_db, level_t_pre_db = (
        solve_branch_trims(
            freqs,
            responses[woofer_role].complex_tf,
            responses[tweeter_role].complex_tf,
            fc_hz,
            woofer_span_hz=woofer_span,
            tweeter_span_hz=tweeter_span,
        )
    )
    _post_res_w, _post_res_t, level_w_post_db, level_t_post_db = solve_branch_trims(
        freqs,
        w_lin,
        t_lin,
        fc_hz,
        woofer_span_hz=woofer_span,
        tweeter_span_hz=tweeter_span,
    )
    level_band_giveback_db = {
        woofer_role: float(level_w_pre_db - level_w_post_db),
        tweeter_role: float(level_t_pre_db - level_t_post_db),
    }
    # The precondition, MEASURED rather than assumed (see the invariant above).
    # The same call that produced the give-back's "before" levels also produced
    # this solve's own trims, so the distance between those and the base the
    # planner was handed is free.
    band_average_trim_db = {
        woofer_role: float(band_average_trim_w_db),
        tweeter_role: float(band_average_trim_t_db),
    }
    polish_delta_db = {
        role: float(request.raw_trim_db.get(role, 0.0) - band_average_trim_db[role])
        for role in (woofer_role, tweeter_role)
    }
    #
    # The anchor's base is the RAW MEASURED TRIM, unconditionally. There is no
    # third term and no branch: the pair is placed where the branch solve
    # measured it, plus each branch's own measured give-back.
    #
    # **Why the summed at-the-mark capture does NOT own this number**, though it
    # is the obvious candidate: the two captures are in different frames BY
    # CONSTRUCTION, so the arithmetic that would combine them double-counts. The
    # per-branch MEASURE sweeps ride the protected-NEUTRAL graph — no crossover,
    # no delay, no linearization, no trims — so ``raw_trim_db`` is an ABSOLUTE
    # per-branch number, while the entry baseline rides the APPLIED incumbent
    # graph, trims included. Reading a per-role level off the incumbent and
    # subtracting it from an absolute trim charges the same attenuation twice:
    # on a flat incumbent with a 10 dB-hot tweeter the derivation returns −20.
    # It would also need the anchor re-placed after the baseline lands, since
    # stage 1 captures it AFTER the fit.
    #
    # The summed capture keeps every role where the frames ARE coherent by
    # construction — VERIFY tracking, the benefit verdict, realization grading —
    # all of which compare summed against summed.
    raw_trim = dict(request.raw_trim_db)
    # The anchor's OTHER term, checked rather than trusted for the reason the
    # request's trims are checked at the door: a non-finite give-back lands in
    # the same ``max`` and the same clamp does nothing about it. It guards
    # ``level_band_giveback_db``, the term the anchor actually spends;
    # ``correction_giveback_db`` is disclosure and cannot reach the clamp.
    giveback_db = {}
    for role in (woofer_role, tweeter_role):
        value = float(level_band_giveback_db[role])
        if not math.isfinite(value):
            raise PlannerInputError(
                f"level-band give-back[{role!r}] is not finite; the anchor's "
                "non-positive normalize cannot clamp a non-finite term"
            )
        giveback_db[role] = value
    anchored, normalize_shift_db = anchor_trims(
        roles=(woofer_role, tweeter_role),
        anchor_base_db=raw_trim,
        giveback_db=giveback_db,
    )
    # NO disclosure here, deliberately. The estimator disagreement has exactly
    # one journal owner — the host's accountability seam
    # (``EVENT_LEVEL_ESTIMATOR_FINDING``), which also banks the finding. This
    # site decides nothing, so a second event would be one fact with two
    # writers and no new information.

    # Ripple fine-tune around the anchor: the anchor sets the LEVEL, the scan
    # only polishes summed flatness near it, and only where the band straddles
    # Fc. On a tweeter swept from Fc the band is ``[Fc, 2*Fc]``, where the
    # woofer sits 20+ dB down its skirt: the summed ripple is then the tweeter's
    # own and barely responds to the tweeter's gain, so the scan is not
    # measuring the handoff. A selector that cannot see the woofer does not set
    # the woofer's handoff level. At the CANDIDATE's corner throughout.
    ripple_lin: float | None = None
    ripple_anchored_lin: float | None = None
    if lo_clamped < fc_hz < hi:
        trim_t_lin, ripple_lin, _seed_lin = solve_ripple_optimal_trim(
            freqs,
            w_lin,
            t_lin,
            fc_hz,
            lo_hz=lo_clamped,
            hi_hz=hi,
            seed_trim_db=anchored[tweeter_role],
            trim_w_db=anchored[woofer_role],
            sign=int(request.polarity_sign),
        )
        # The scan's own objective, re-read at the trim that SHIPS. Same
        # branches, same band, same statistic, same :func:`ripple_at_trim` the
        # scan evaluates every candidate with, so the tweeter trim is the only
        # thing that moves between this number and ``ripple_lin`` and the
        # rejection telemetry below is a controlled comparison. Finite whenever
        # it is reached: the scan raises on a band with no bins.
        ripple_anchored_lin = ripple_at_trim(
            freqs,
            w_lin,
            t_lin,
            lo_hz=lo_clamped,
            hi_hz=hi,
            trim_w_db=anchored[woofer_role],
            trim_t_db=anchored[tweeter_role],
            sign=int(request.polarity_sign),
        )
    else:
        emit(
            "correction.crossover_v2_linearization_ripple_trim_skipped",
            {
                "reason": "ripple_band_one_sided",
                "fc_hz": round(float(fc_hz), 3),
                "ripple_band_hz": (round(float(lo_clamped), 1), round(float(hi), 1)),
                "anchored_trim_db": {k: round(v, 3) for k, v in anchored.items()},
            },
        )
        trim_t_lin = anchored[tweeter_role]
    resolved = {
        woofer_role: anchored[woofer_role],
        tweeter_role: float(trim_t_lin),
    }

    emit(
        "correction.crossover_v2_linearization_giveback",
        {
            # THE ANCHOR'S TERM: the level-band give-back, measured by the same
            # estimator over the same bands the committed pair is graded in.
            # Named for its BAND, so it cannot be confused with the core-band
            # give-back published below it.
            "level_band_giveback_db": {
                role: round(float(giveback_db[role]), 3)
                for role in (woofer_role, tweeter_role)
            },
            # THE PRECONDITION, published so it is observed on every real round
            # rather than assumed. The give-back is calibrated to a base that
            # came from the band-average solve; when MEASURE polished the trim
            # for ripple instead, this is how far the base moved, and the pair
            # lands with exactly that much realized inter-driver level error.
            # Zero on the ordinary path.
            "band_average_trim_db": {
                role: round(band_average_trim_db[role], 3)
                for role in (woofer_role, tweeter_role)
            },
            "polish_delta_db": {
                role: round(polish_delta_db[role], 3)
                for role in (woofer_role, tweeter_role)
            },
            # The core-band give-back, kept BESIDE it rather than replacing it.
            # It answers the audible-band question — what the correction removed
            # across the driver's own passband — and places no trim. Publishing
            # both makes a band mismatch visible in one line: when a driver's
            # correction is concentrated outside the graded band the two
            # diverge, and their per-role DIFFERENCE is inter-driver error.
            "core_band_giveback_db": {
                role: round(float(fits[role].correction_giveback_db), 3)
                for role in (woofer_role, tweeter_role)
            },
            "raw_trim_db": {k: round(v, 3) for k, v in raw_trim.items()},
            "anchored_trim_db": {k: round(v, 3) for k, v in anchored.items()},
            "normalize_shift_db": round(float(normalize_shift_db), 3),
            # The FIT frame's own per-role level, beside the TRIM frame this
            # line already carries. ``raw_trim_db`` should track the negated
            # difference of these two; a large disagreement means the level
            # match and the fit are measuring different things.
            "target_level_db": {
                role: round(float(fits[role].target_level_db), 3)
                for role in (woofer_role, tweeter_role)
            },
            # Whether the two level DEFINITIONS parted company past the
            # disclosure trigger. Neither moved ``anchored_trim_db`` and
            # neither can — the anchor is the raw measured trim. ``None``
            # means one of them covered no role.
            "level_definitions_differ": (
                None if level_consistency is None else level_consistency.differs
            ),
            "level_estimator_worst_delta_db": (
                None
                if level_consistency is None
                else round(float(level_consistency.worst_delta_db), 3)
            ),
        },
    )

    # --- grade both pairs, then commit one ---------------------------------
    #
    # At the CANDIDATE's corner, on BOTH pairs.
    anchored_match = realized_level_match(
        freqs,
        w_lin,
        t_lin,
        fc_hz,
        anchored,
        woofer_role,
        tweeter_role,
        woofer_span_hz=woofer_span,
        tweeter_span_hz=tweeter_span,
    )
    resolved_match = realized_level_match(
        freqs,
        w_lin,
        t_lin,
        fc_hz,
        resolved,
        woofer_role,
        tweeter_role,
        woofer_span_hz=woofer_span,
        tweeter_span_hz=tweeter_span,
    )
    trim = decide_trim(
        anchored_db=anchored,
        resolved_db=resolved,
        tweeter_role=tweeter_role,
        anchored_match=anchored_match,
        resolved_match=resolved_match,
        ripple_db=ripple_lin,
        sanity_margin_db=request.trim_sanity_margin_db,
    )
    role_attenuations_db = dict(trim.committed_db)

    if trim.beyond_sanity_margin:
        emit(
        "correction.crossover_v2_linearization_trim_rejected",
        {
            "raw_trim_db": {k: round(v, 3) for k, v in raw_trim.items()},
            "resolved_trim_db": {
                k: round(v, 3) for k, v in trim.resolved_db.items()
            },
            # The anchor the guard is measured against, and the pair that ships.
            "anchored_trim_db": {
                k: round(v, 3) for k, v in trim.anchored_db.items()
            },
            "fallback_trim_db": {
                k: round(v, 3) for k, v in role_attenuations_db.items()
            },
            "anchored_level_error_db": round(
                float(anchored_match.difference_db), 3
            ),
            "resolved_level_error_db": round(
                float(resolved_match.difference_db), 3
            ),
            # Derived, not restated: a literal here would be a second place
            # recording which pair won, free to drift from the one that
            # decided it.
            "committed": trim.committed_side,
            "strategy": trim.strategy.value,
            "drift_db": round(float(trim.anchor_drift_db), 3),
            "margin_db": trim.sanity_margin_db,
            # The ripple at each trim, so live evidence can distinguish
            # "legitimate flatter optimum rejected" from "garbage correctly
            # caught" before anyone widens the guard. ``None`` is unreachable
            # on both linearized fields — a skipped scan leaves the trim AT
            # the anchor, so the drift is 0 and this event does not fire —
            # but they stay honest rather than reporting a fabricated 0.0.
            #
            # **Read the first two against each other, never either against
            # the third.** ``anchored_ripple_db`` and ``resolved_ripple_db``
            # are the SAME linearized branches over the SAME band through the
            # same ``ripple_at_trim``, differing only in the tweeter trim, so
            # their difference is exactly what the guard cost this candidate
            # in flatness. ``raw_predicted_ripple_db`` is the RAW pre-fit
            # branches at the MEASURE trim — a different curve at a different
            # stage — so pairing it with a linearized ripple moves two
            # variables at once and reads the linearization itself as
            # flatness thrown away.
            "anchored_ripple_db": (
                round(float(ripple_anchored_lin), 3)
                if ripple_anchored_lin is not None
                else None
            ),
            "resolved_ripple_db": (
                round(float(trim.ripple_db), 3)
                if trim.ripple_db is not None
                else None
            ),
            "raw_predicted_ripple_db": round(
                float(request.predicted_ripple_db), 3
            ),
        },
        level=logging.WARNING,
        )

    # The inter-driver realized-level ledger, on every fitted candidate whatever
    # the guard decided. Recorded here, where the linearized branches live, and
    # GRADED at the host's accountability seam — deliberately outside the host's
    # degrade-to-trims-only catch, so the verdict is reached and banked on the
    # candidate that was actually built rather than swallowed into the
    # unlinearized path.
    committed_match = trim.committed_match
    emit(
    "correction.crossover_v2_realized_level_match",
    {
        "matched": committed_match.matched,
        "level_w_db": round(float(committed_match.level_w_db), 3),
        "level_t_db": round(float(committed_match.level_t_db), 3),
        "difference_db": round(float(committed_match.difference_db), 3),
        "tolerance_db": committed_match.tolerance_db,
        "woofer_band_hz": tuple(
            round(v, 1) for v in committed_match.woofer_band_hz
        ),
        "tweeter_band_hz": tuple(
            round(v, 1) for v in committed_match.tweeter_band_hz
        ),
        "trim_db": {k: round(v, 3) for k, v in role_attenuations_db.items()},
    },
    level=logging.WARNING if not committed_match.matched else logging.INFO,
    )

    # VERIFY-prediction coherence: the emitted graph carries these SAME
    # W_lin/T_lin correction filters whichever trim pair was committed — the
    # sanity guard only ever changes the TRIM, never whether the filters are
    # emitted — so the persisted VERIFY prediction is rebuilt from them too, at
    # whichever trim ``role_attenuations_db`` ended up holding. Comparing a
    # correctly-linearized measured summation against a prediction built from
    # the raw branches is a deterministic mismatch equal to the filters' own
    # in-band response.
    #
    # ``w_lin``/``t_lin`` are the branches' ``DriverResponse.complex_tf`` times
    # a correction, windowed on each branch's OWN argmax exactly as
    # ``_aligned_branch_tf`` does, so this pair sits in the SAME
    # argmax-referenced frame as the raw pair and takes the SAME residual —
    # never the applied delay itself, which would double-count the measured peak
    # gap. Both numbers come off the alignment the host read, the same pair
    # ``alignment_to_candidate_fields`` uses to build the emitted
    # ``MeasuredCrossoverAlignment``.
    #
    # The frame keeps the prediction about the SHIPPING filters rather than the
    # fitted ones: a per-driver prescription may replace a role's filters after
    # this function returns, and ``planning.build_candidate`` recomposes through
    # the same :func:`compose_linearized_prediction`.
    summation_frame = SummationFrame(
        freqs_hz=freqs,
        branch_tf={
            woofer_role: responses[woofer_role].complex_tf,
            tweeter_role: responses[tweeter_role].complex_tf,
        },
        polarity_sign=int(request.polarity_sign),
        residual_delay_us=summed_model_residual_delay_us(
            request.anchor_delay_us, request.delay_us
        ),
    )
    return assemble_plan(
        fitted,
        role_attenuations_db=role_attenuations_db,
        trim=trim,
        polish_delta_db=polish_delta_db,
        summation_frame=summation_frame,
        linearized_predicted_sum=compose_linearized_prediction(
            summation_frame,
            filters_by_role={
                role: [f.to_dict() for f in fits[role].filters]
                for role in (woofer_role, tweeter_role)
            },
            role_attenuations_db=role_attenuations_db,
        ),
        emit=emit,
        records=records,
        dropped=dropped,
    )


def request_from_analysis(
    analysis: Any,
    candidate: Any,
    *,
    context: CandidateAcousticContext | None,
    roles: Sequence[str],
    excited_band_hz: Mapping[str, tuple[float, float]],
    driver_class_by_role: Mapping[str, str],
    post_apply_verifies: bool,
    cloud_phase_planned: bool,
    cloud: Any = None,
) -> LinearizationRequest:
    """Assemble a request from a ``ProgramAnalysis`` and its raw candidate.

    The one place the host's measurement objects are unpacked into the
    planner's explicit inputs. A *derivation*, not a policy: every value is read
    straight off the analysis, and the two facts the analysis cannot know
    (``post_apply_verifies``, ``cloud_phase_planned``) are the host's to pass.

    Raises :class:`PlannerInputError` when the analysis lacks a driver response
    for a role, or when a PAIR carries no alignment — both of which the host's
    own eligibility gate is supposed to have excluded, so reaching them means
    the gate and this derivation disagree and the plan must not be guessed at.
    A 1-way analysis legitimately carries no alignment and no candidate; the
    absences travel as the neutral values the plan never reads.
    """
    alignment = analysis.alignment
    if alignment is None and len(roles) > 1:
        raise PlannerInputError("a MEASURE analysis must carry an alignment")
    drivers: dict[str, DriverEvidence] = {}
    for role in roles:
        response = driver_response_by_role(analysis, role)
        if response is None:
            raise PlannerInputError(f"no measured response for role {role!r}")
        band = excited_band_hz.get(role)
        if band is None:
            raise PlannerInputError(f"no excited band for role {role!r}")
        drivers[role] = DriverEvidence(
            role=role,
            response=response,
            excited_band_hz=(float(band[0]), float(band[1])),
            driver_class=str(driver_class_by_role.get(role, "unknown")),
        )
    return LinearizationRequest(
        context=context,
        drivers=tuple(drivers[role] for role in roles),
        raw_trim_db={} if candidate is None else dict(candidate.trim_db),
        trim_band_average_db=(
            {} if candidate is None else dict(candidate.trim_band_average_db or {})
        ),
        predicted_ripple_db=(
            0.0 if candidate is None else float(candidate.predicted_ripple_db)
        ),
        polarity_sign=1 if alignment is None else int(alignment.polarity_sign),
        delay_us=0.0 if alignment is None else float(alignment.delay_us),
        # ``None`` when the alignment is not OK is load-bearing, not a default:
        # ``summed_model_residual_delay_us`` treats an absent anchor differently
        # from a zero one. The candidate's own ``anchor_delay_us`` is NOT used —
        # it is ``None`` on the no-declared-bounds path where the alignment's is
        # not, and the two models must not disagree.
        anchor_delay_us=(
            alignment.anchor_delay_us
            if alignment is not None and alignment.status == ALIGNMENT_OK
            else None
        ),
        mic_tier=str(analysis.mic_tier),
        branch_floor_hz=measure_validity_floor_hz(analysis),
        post_apply_verifies=bool(post_apply_verifies),
        cloud_phase_planned=bool(cloud_phase_planned),
        cloud=CloudFitTerms.from_evidence(cloud),
    )
