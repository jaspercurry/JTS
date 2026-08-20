# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which crossovers are worth measuring, ranked, with no tone played.

The last offline module: :mod:`.candidate_space` says what is legal,
:mod:`.forward_model` says what a legal candidate will measure, :mod:`.objective`
says what that is worth, and this walks the space and returns a shortlist.

**Shape: exhaustive outer, bounded-grid inner.** The discrete axes — LR order
and branch polarity — are a cross product of six for a two-way, so they are a
``for`` loop rather than a search problem. Filter TYPE is not an axis:
:data:`~jasper.active_speaker.profile.SUPPORTED_CROSSOVER_TYPES` is a
one-element set and a non-Linkwitz-Riley candidate is not representable. Inside
each branch, ``(Fc, tau, gain)`` is scanned coarse then fine.

**Why a grid and not an optimizer, from what the repo already has.**
``scipy.optimize`` appears exactly twice in ``jasper/`` — ``least_squares`` in
the two ``bass_extension`` adapters, fitting continuous physical box parameters
to a measured curve. ``minimize``, ``curve_fit``, ``brentq`` and golden-section
appear nowhere, and ``scipy.stats`` appears nowhere at all. What DOES exist is
one idiom, implemented independently three times for three different variables:
``program_analysis.solve_ripple_optimal_trim`` (trim, +/-10 dB at 0.1 dB),
``program_analysis._select_alignment_pair`` (polarity x delay, 10 us steps over
one period at Fc) and ``fc_sweep.fc_candidate_set`` (Fc, geometric). All three
are bounded grids, and two of them regularize the same way: among every
candidate within an epsilon of the best score, take the one closest to the
incumbent. This module adopts that idiom rather than importing a solver, for
three reasons that are properties of this problem and not preferences:

* **the objective is not smooth** — it contains banded tolerances and
  worst-deviation terms, and a local method on a non-smooth multi-modal
  landscape is exactly where a local optimum passes for an answer;
* **it is affordable** — every evaluation is array arithmetic on banked curves,
  no speaker time at any grid size, so there is no budget pressure to be clever;
* **it produces a LANDSCAPE, not a point**, which is what a bracket-then-measure
  method needs — and bracketing is all the model is currently allowed to do.

**Determinism is structural, not seeded.** Nothing here draws a random number,
so there is no seed to set and none to forget: the grids are closed-form, the
iteration order is sorted, and ties break through a stated lexicographic rule.
Two runs over the same inputs produce byte-identical output, and a test says so.

**THE H5 GUARD — the model has no ranking authority for the delay lever.**
Re-graded against the banked armrun with the delay-SENSITIVE output, the forward
model is perfectly ANTI-correlated with the pooled measured referee: Spearman
rho = -1.000 across 6 arms and 5 distinct delays. The pre-registered bar for a
ranking vote is rho >= +0.6, so the bar fails, decisively and in the wrong
direction. Until a calibration rung clears it this search may say WHICH
candidates are worth measuring and may not say which is best, and
:data:`DELAY_RANKING_AUTHORITY` rides every :class:`Shortlist` and every
prediction document so a consumer reading the artifact alone still sees it. It
is printed, not merely documented, because a caveat that lives only in prose is
a caveat that goes unread.

Units are in every name: ``_hz``, ``_us``, ``_db``, ``_deg``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_FLAT_MINIMUM_EPSILON_DB,
)

from ..branch_chain import CrossoverSection
from .candidate_space import CandidateBounds, XoverCandidate, refusal_for
from .forward_model import DriverPlant, predict_sum
from .objective import (
    ADVISORY_WEIGHTS, Domination, FlatnessProfile, GradingFrame, ObjectiveScore,
    ObjectiveWeights, dominates, score_prediction,
)

__all__ = [
    "COARSE_DELAY_STEPS",
    "COARSE_FC_STEPS",
    "COARSE_GAIN_STEPS",
    "DELAY_RANKING_AUTHORITY",
    "DELAY_RANKING_BAR_RHO",
    "DELAY_RANKING_EVIDENCE",
    "DELAY_RANKING_MEASURED_RHO",
    "FLAT_MINIMUM_EPSILON_DB",
    "GAIN_SEARCH_STEP_DB",
    "GAIN_SEARCH_WINDOW_DB",
    "RANKING_AUTHORITY_BRACKET_ONLY",
    "RANKING_AUTHORITY_RANK",
    "REFINEMENT_FACTOR",
    "RankedCandidate",
    "RankingAuthority",
    "SearchPlan",
    "Shortlist",
    "prediction_document",
    "search_candidates",
    "spearman_rho",
]

#: This search may choose which candidates are worth MEASURING.
RANKING_AUTHORITY_BRACKET_ONLY = "bracket_only"
#: This search may say which candidate is BEST. Not currently reachable.
RANKING_AUTHORITY_RANK = "rank"

#: The rank correlation a lever's predictions must reach to earn a ranking vote.
#:
#: Pre-registered before the run that measured it, which is what makes it a bar
#: rather than a description. Spearman rather than Pearson because the claim
#: under test is about ORDER — "the model ranks the arms the way the speaker
#: does" — and a monotone relationship with a curved shape would still be a
#: model worth ranking with.
DELAY_RANKING_BAR_RHO: float = 0.6

#: What the banked armrun actually measured for the delay lever.
#:
#: PERFECT anti-correlation, not merely a miss: the model's best-predicted arm
#: measured worst pooled and its worst-predicted measured best, strictly
#: monotone with no ties. Read it as one clean directional trend across a 562 us
#: span with 5 distinct doses rather than as six independent confirmations —
#: the arms are a dose-response series, not exchangeable samples, so the exact
#: permutation p overstates the evidence even though the direction is
#: unambiguous.
#:
#: **This number names a REFEREE, and a different one answers differently.** It
#: is the POOLED five-position measurement. Against the on-axis MARK, the same
#: artifact reports rho = **+0.6571** self-referenced across all six arms, which
#: would CLEAR the bar above. That is not a reason to adopt the mark: the same
#: analysis swings to **-1.0000** on the frozen reference over the invert-only
#: subset, and the artifact's own verdict on it is that "a statistic that swings
#: from +0.66 to -1.00 across reasonable analysis choices is not evidence in
#: either direction — at the mark, this bank cannot answer the question." The
#: pooled axis is taken because it is the campaign's own referee and it is
#: stable, and the mark figure is recorded here so nobody rediscovers it later
#: and reads a passing grade off it. The two measured axes anti-correlate with
#: each other at rho = -0.66, which is the lobe re-aiming this module's
#: directivity term exists to charge for.
DELAY_RANKING_MEASURED_RHO: float = -1.0

#: Where :data:`DELAY_RANKING_MEASURED_RHO` came from, so the number can be
#: re-derived rather than believed.
DELAY_RANKING_EVIDENCE = (
    "captures/xover-armrun-2026-08-18/analysis/README-delay-arm-regrade.md "
    "(2026-08-19): 6 arms, 5 distinct delays, the delay-sensitive predicted sum "
    "against the pooled 5-position measured referee, both sides through one "
    "identical reduction validated against evaluate_flat_spec to 1.1e-16 dB"
)

#: How close to the best score still counts as the same minimum, dB.
#:
#: Imported rather than chosen: this is the epsilon
#: ``program_analysis._select_alignment_pair`` regularizes its own polarity-x-delay
#: grid with, and it was calibrated against a ripple-shaped objective in the same
#: dB units as this one. Bare ``argmin`` on a shallow bowl chases measurement
#: noise; inside the flat region the honest answer is "these are the same", and
#: the tie-break belongs to the incumbent.
FLAT_MINIMUM_EPSILON_DB: float = ALIGNMENT_FLAT_MINIMUM_EPSILON_DB

#: Coarse-pass grid sizes, per branch.
#:
#: Sized so a two-way's whole coarse pass is a few hundred evaluations rather
#: than a few thousand: the delay axis is usually the widest of the three
#: because it is walked on the chain's own realizable lattice (below), so the
#: other two stay deliberately coarse and the refinement pass buys the
#: resolution back where it matters.
COARSE_FC_STEPS: int = 7
COARSE_DELAY_STEPS: int = 9
COARSE_GAIN_STEPS: int = 5

#: How much finer the refinement pass is, per axis.
REFINEMENT_FACTOR: int = 4

#: The relative-trim window the gain axis scans, and its coarse step, dB.
#:
#: A window and not a range: the absolute level is already solved, and what this
#: axis moves is the residual mismatch between branches after that solve. Two dB
#: either side covers the trim error a level solve realistically leaves without
#: letting the search re-do the solve's job.
GAIN_SEARCH_WINDOW_DB: float = 2.0
GAIN_SEARCH_STEP_DB: float = 0.5


@dataclass(frozen=True)
class RankingAuthority:
    """What this model is currently allowed to claim about one lever.

    Carried on every shortlist and printed into every prediction document. A
    consumer that reads the artifact and not the docs still cannot miss it,
    which is the point: the campaign's original delay conclusion was drawn from
    a number whose own docstring said it must not be used that way.
    """

    lever: str
    bar_rho: float
    measured_rho: float | None
    authority: str
    evidence: str

    @property
    def may_rank(self) -> bool:
        """Whether a shortlist ORDER is a claim about the speaker."""
        return self.authority == RANKING_AUTHORITY_RANK

    def to_dict(self) -> dict[str, Any]:
        return {
            "lever": self.lever,
            "bar_rho": self.bar_rho,
            "measured_rho": self.measured_rho,
            "authority": self.authority,
            "evidence": self.evidence,
        }


def _authority_for(measured_rho: float | None, bar_rho: float) -> str:
    """``rank`` only when the measurement CLEARS the bar; otherwise bracket.

    An unmeasured lever brackets too. Absence of evidence is not a passing
    grade, and the fail-closed direction is the only one where being wrong
    costs a measurement rather than a household's speaker.
    """
    if measured_rho is None or not math.isfinite(measured_rho):
        return RANKING_AUTHORITY_BRACKET_ONLY
    if measured_rho >= bar_rho:
        return RANKING_AUTHORITY_RANK
    return RANKING_AUTHORITY_BRACKET_ONLY


#: The delay lever's standing verdict, derived rather than asserted.
DELAY_RANKING_AUTHORITY = RankingAuthority(
    lever="delay",
    bar_rho=DELAY_RANKING_BAR_RHO,
    measured_rho=DELAY_RANKING_MEASURED_RHO,
    authority=_authority_for(DELAY_RANKING_MEASURED_RHO, DELAY_RANKING_BAR_RHO),
    evidence=DELAY_RANKING_EVIDENCE,
)


def spearman_rho(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Spearman rank correlation of two equal-length samples, or ``None``.

    Written here rather than imported because ``scipy.stats`` appears nowhere in
    ``jasper/`` and this is the one statistic the calibration bar is defined
    against — a whole dependency surface for one closed-form line would be a
    poor trade, and the grading stack this sits beside states numpy-only purity
    as a contract.

    Ties take average ranks, which is the standard definition and the one the
    banked analysis used. ``None`` when fewer than two pairs survive or when
    either side is constant — a constant has no ranking, and returning 0.0 for
    one would read as "measured, and unrelated".
    """
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    if a.shape != b.shape or a.size < 2:
        return None
    usable = np.isfinite(a) & np.isfinite(b)
    if int(usable.sum()) < 2:
        return None
    ranked_a, ranked_b = _average_ranks(a[usable]), _average_ranks(b[usable])
    da, db = ranked_a - ranked_a.mean(), ranked_b - ranked_b.mean()
    denominator = math.sqrt(float(np.dot(da, da)) * float(np.dot(db, db)))
    if denominator <= 0.0:
        return None
    return float(np.dot(da, db) / denominator)


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks with ties averaged."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = np.arange(1, values.size + 1, dtype=np.float64)
    for value in np.unique(values):
        tied = values == value
        if int(tied.sum()) > 1:
            ranks[tied] = ranks[tied].mean()
    return ranks


@dataclass(frozen=True)
class SearchPlan:
    """What the walk may vary, and how finely.

    Everything here is declared by the caller from the session it is proposing
    into. The module invents no bound: ``bounds`` owns legality,
    ``incumbent`` owns where the tie-break points, and these numbers own only
    resolution.

    ``fc_steps`` / ``delay_steps`` / ``gain_steps`` are the COARSE pass. The
    refinement pass re-scans each axis at :data:`REFINEMENT_FACTOR` times the
    resolution across one coarse cell either side of the coarse winner.

    ``delay_steps`` is a CEILING rather than a count: the delay axis is walked
    on the chain's own realizable lattice
    (:attr:`~jasper.active_speaker.crossover_v2.candidate_space.CandidateBounds.delay_step_us`,
    #2710's +/-20.833 us at 48 kHz), because a finer proposal is a number the
    emitted graph rounds away and
    :func:`~jasper.active_speaker.crossover_v2.candidate_space.refusal_for`
    would refuse it by name. When the lattice offers fewer points than this
    ceiling, the lattice wins.
    """

    hf_role: str
    lf_role: str
    fc_steps: int = COARSE_FC_STEPS
    delay_steps: int = COARSE_DELAY_STEPS
    gain_steps: int = COARSE_GAIN_STEPS
    gain_window_db: float = GAIN_SEARCH_WINDOW_DB
    refinement: int = REFINEMENT_FACTOR
    shortlist_size: int = 5


@dataclass(frozen=True)
class RankedCandidate:
    """One evaluated candidate: what it is, what it scores, and how it compares."""

    candidate: XoverCandidate
    score: ObjectiveScore
    domination: Domination | None

    @property
    def profile_by_view(self) -> Mapping[str, FlatnessProfile]:
        """Every view's flatness profile, keyed by view — the acceptance object."""
        return {view.view: view.profile for view in self.score.views}

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": _candidate_dict(self.candidate),
            "score": self.score.to_dict(),
            "domination": (
                None if self.domination is None else self.domination.to_dict()
            ),
        }


@dataclass(frozen=True)
class Shortlist:
    """The ranked few, the incumbent, and everything the walk refused.

    ``incumbent`` is always present and always evaluated — ``select_fc``'s S9.8
    discipline kept verbatim: the configured candidate is in the evaluated set,
    an alternative must beat it, and keeping configured is an honest verdict.
    It is NOT in ``ranked`` unless it earned a place there on its own score.

    ``authority`` is the H5 stamp. While it is
    :data:`RANKING_AUTHORITY_BRACKET_ONLY`, ``ranked`` is a list of candidates
    worth MEASURING and its ORDER is not a claim about the speaker.
    """

    ranked: tuple[RankedCandidate, ...]
    incumbent: RankedCandidate
    bounds: CandidateBounds
    authority: RankingAuthority
    landscape: Mapping[str, Any]
    refusals: tuple[tuple[str, str], ...]
    evaluated: int


def _candidate_dict(candidate: XoverCandidate) -> dict[str, Any]:
    """A candidate as JSON, in the emitted graph's own vocabulary."""
    return {
        "sections_by_role": {
            role: [
                {
                    "fc_hz": float(section.fc_hz),
                    "order": int(section.order),
                    "highpass": bool(section.highpass),
                }
                for section in sections
            ]
            for role, sections in sorted(candidate.sections_by_role.items())
        },
        "polarity_by_role": {
            role: int(value)
            for role, value in sorted(candidate.polarity_by_role.items())
        },
        "delay_us_by_role": {
            role: float(value)
            for role, value in sorted(candidate.delay_us_by_role.items())
        },
        "gain_db_by_role": {
            role: float(value)
            for role, value in sorted(candidate.gain_db_by_role.items())
        },
    }


def _geometric_steps(band_hz: tuple[float, float], steps: int) -> tuple[float, ...]:
    """``steps`` corners spanning ``band_hz``, evenly spaced in octaves.

    Geometric because a crossover argument is a per-octave one — the same reason
    ``fc_sweep.fc_candidate_set`` gives for its own spacing. A linear sweep would
    spend most of its samples in the top octave of the band, which is the half a
    crossover choice cares about least.
    """
    lo_hz, hi_hz = band_hz
    if steps < 1 or not (0.0 < lo_hz <= hi_hz):
        return ()
    if steps == 1 or lo_hz == hi_hz:
        return (float(lo_hz),)
    ratio = (hi_hz / lo_hz) ** (1.0 / (steps - 1))
    return tuple(float(lo_hz * ratio ** index) for index in range(steps))


def _lattice_steps(
    window_us: tuple[float, float], step_us: float, ceiling: int,
) -> tuple[float, ...]:
    """Realizable delays inside ``window_us``, at most ``ceiling`` of them.

    On a declared lattice the values are the lattice's own multiples, so every
    proposal is a delay the emitted graph can actually spell. With no declared
    lattice (``step_us <= 0``) the window is divided evenly, because refusing to
    propose any delay at all would be inventing a timing bound the caller did
    not state — the same posture ``candidate_space._delay_refused_by_step``
    takes.
    """
    lo_us, hi_us = float(window_us[0]), float(window_us[1])
    if hi_us < lo_us or ceiling < 1:
        return ()
    if step_us <= 0.0:
        if ceiling == 1 or hi_us == lo_us:
            return (lo_us,)
        span = (hi_us - lo_us) / (ceiling - 1)
        return tuple(lo_us + span * index for index in range(ceiling))
    first = math.ceil(lo_us / step_us)
    last = math.floor(hi_us / step_us)
    if last < first:
        return ()
    multiples = list(range(first, last + 1))
    if len(multiples) > ceiling:
        # Thin by taking every k-th lattice point, never by moving one: a
        # thinned grid must still land ON the lattice or the search proposes
        # delays the chain rounds away.
        stride = math.ceil(len(multiples) / ceiling)
        multiples = multiples[::stride]
    return tuple(float(multiple * step_us) for multiple in multiples)


def _gain_steps(
    center: float,
    half_width: float,
    steps: int,
    range_db: tuple[float, float],
) -> tuple[float, ...]:
    """``steps`` trims around ``center``, INSIDE the declared range.

    The window is intersected with the declared range before it is stepped,
    rather than stepped and then refused. Those are different things and only
    one is right: refusing a proposal outside a bound is the front door's job
    and stays a named refusal, but GENERATING points a bound already forbids
    spends half the axis on candidates that cannot exist. On a two-way whose
    trim range tops out at unity — which is every one of them, since the range
    is at-or-below-unity by construction — an unintersected window puts half
    its samples above 0 dB and refuses all of them.

    Empty only when the range itself is empty; a range that excludes the centre
    still yields its own edge, because the nearest legal trim is a candidate
    worth scoring even when the incumbent's is not.
    """
    lo_db = max(float(range_db[0]), float(center) - float(half_width))
    hi_db = min(float(range_db[1]), float(center) + float(half_width))
    if steps < 1 or hi_db < lo_db:
        return ()
    if steps == 1 or hi_db == lo_db:
        return (float(lo_db),)
    span = (hi_db - lo_db) / (steps - 1)
    return tuple(float(lo_db + span * index) for index in range(steps))


def _sections_for(
    *, fc_hz: float, order: int, hf_role: str, lf_role: str,
) -> dict[str, tuple[CrossoverSection, ...]]:
    """A two-way's sections: high-pass on the HF role, low-pass on the LF role.

    One corner shared by both branches, which is what a Linkwitz-Riley pair IS —
    two halves of one handoff, not two independent filters. Written as the same
    float on both sides so
    :attr:`~jasper.active_speaker.crossover_v2.candidate_space.XoverCandidate.corner_hz`
    sees one corner rather than two rounded ones.
    """
    return {
        hf_role: (CrossoverSection(fc_hz=fc_hz, order=order, highpass=True),),
        lf_role: (CrossoverSection(fc_hz=fc_hz, order=order, highpass=False),),
    }


def _candidate_at(
    *,
    fc_hz: float,
    order: int,
    polarity: int,
    delay_us: float,
    gain_db: float,
    plan: SearchPlan,
    incumbent: XoverCandidate,
) -> XoverCandidate:
    """One point of the grid, as a candidate.

    The LF role keeps the incumbent's own delay, polarity and trim: only the
    RELATIVE terms are searchable, because a common-mode delay is a latency
    change rather than a crossover change and a common-mode trim is the level
    solve's business. Searching both branches independently would let the walk
    spend its budget on pairs that differ only by a constant.

    **This shape high-passes the HF role and nothing else, and that is a bound
    rather than an oversight.** A floor declared on the LF role, or on any role
    a two-way plan never names, therefore has no corner in this shape to answer
    it, and the search may not invent one: proposing an unprotected branch to
    get around a driver's declaration is the failure the front door exists to
    stop.

    What answers such a floor is the chain, not the candidate. The caller
    declares what each branch already high-passes at outside the crossover on
    :attr:`~jasper.active_speaker.crossover_v2.candidate_space.CandidateBounds.standing_highpass_hz_by_role`
    — a two-way woofer's bass-management high-pass sits ahead of the split — and
    the front door credits it for exactly the roles this shape cannot cover
    (#2760; reading the crossover corner alone refused 1681 of 1681 candidates
    on a confirmed profile). When the chain carries nothing there, every
    candidate is still refused by name, and what the search owes then is
    legibility rather than a workaround: the refusal is on every point and the
    incumbent is still graded, so a household running that branch sees it.
    """
    return XoverCandidate(
        sections_by_role=_sections_for(
            fc_hz=fc_hz, order=order, hf_role=plan.hf_role, lf_role=plan.lf_role,
        ),
        polarity_by_role={
            plan.hf_role: polarity,
            plan.lf_role: int(incumbent.polarity_by_role.get(plan.lf_role, 1)),
        },
        delay_us_by_role={
            plan.hf_role: delay_us,
            plan.lf_role: float(incumbent.delay_us_by_role.get(plan.lf_role, 0.0)),
        },
        gain_db_by_role={
            plan.hf_role: gain_db,
            plan.lf_role: float(incumbent.gain_db_by_role.get(plan.lf_role, 0.0)),
        },
    )


@dataclass(frozen=True)
class _GridPoint:
    """One grid point's coordinates, kept beside its score for the tie-break."""

    fc_hz: float
    order: int
    polarity: int
    delay_us: float
    gain_db: float


def _incumbent_distance(
    point: _GridPoint, incumbent: _GridPoint,
) -> tuple[int, int, float, float, float]:
    """How far one grid point sits from the incumbent, lexicographically.

    A tuple rather than a scalar on purpose: octaves, microseconds and decibels
    have no exchange rate, and inventing one would hide a policy inside a norm.
    Order and polarity come first because they are the SHAPE of the crossover
    and a household is being asked to accept a different filter, not a nudged
    one; ``_select_alignment_pair`` prefers the seed's polarity for the same
    reason. Then the corner in octaves, then the delay, then the trim.
    """
    return (
        0 if point.order == incumbent.order else 1,
        0 if point.polarity == incumbent.polarity else 1,
        abs(math.log2(point.fc_hz / incumbent.fc_hz)) if incumbent.fc_hz > 0 else 0.0,
        abs(point.delay_us - incumbent.delay_us),
        abs(point.gain_db - incumbent.gain_db),
    )


def _magnitudes_by_angle(
    predicted: Mapping[float, np.ndarray],
) -> dict[float, np.ndarray]:
    """``20*log10|S|`` per angle, with an exact zero left as ``-inf``.

    Not clamped: a bin the model says is silent is silent, and
    :func:`~jasper.active_speaker.crossover_v2.objective.flatness_profile`
    drops non-finite bins rather than grading a floor somebody chose here.
    """
    magnitudes: dict[float, np.ndarray] = {}
    for angle_deg, response in predicted.items():
        values = np.abs(np.asarray(response, dtype=np.complex128))
        with np.errstate(divide="ignore"):
            magnitudes[float(angle_deg)] = 20.0 * np.log10(values)
    return magnitudes


def search_candidates(
    plants: Sequence[DriverPlant],
    bounds: CandidateBounds,
    incumbent: XoverCandidate,
    *,
    plan: SearchPlan,
    role_by_angle: Mapping[float, str],
    primary_role: str,
    on_axis_deg: float,
    frame_by_angle: Mapping[float, GradingFrame],
    linearization_by_role: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    headroom_charge_db_for: Any = None,
    weights: ObjectiveWeights = ADVISORY_WEIGHTS,
    authority: RankingAuthority = DELAY_RANKING_AUTHORITY,
) -> Shortlist:
    """Walk the legal space and return the candidates worth measuring.

    ``plants`` are :func:`~jasper.active_speaker.crossover_v2.forward_model.driver_plants`'
    output — candidate-independent, computed once, which is the whole reason
    this runs offline. ``bounds`` and ``incumbent`` come from the session.
    ``headroom_charge_db_for`` is an optional ``candidate -> float`` the caller
    supplies (``branch_chain.branch_headroom_db`` needs the emitted filter list,
    which is the caller's to assemble); absent, headroom is charged at zero and
    the weight has nothing to bite on.

    Every point is checked by
    :func:`~jasper.active_speaker.crossover_v2.candidate_space.refusal_for`
    before it is scored, and a refused point is recorded in ``refusals`` by name
    rather than dropped — a search that silently skipped its own bounds would
    make an empty shortlist indistinguishable from a search that found nothing
    good.

    The incumbent is evaluated even when it is itself out of bounds, and that is
    deliberate: a speaker running a corner its declarations no longer permit is
    exactly the case where a household most needs to see what it is running
    graded beside the alternatives. Its refusal is recorded.

    Ranking is by advisory total, ties broken by distance from the incumbent
    (:func:`_incumbent_distance`) so a flat minimum resolves to the smallest
    change rather than to whichever grid point numpy visited first.
    """
    refusals: list[tuple[str, str]] = []
    evaluate = _evaluator(
        plants=plants,
        role_by_angle=role_by_angle,
        primary_role=primary_role,
        on_axis_deg=on_axis_deg,
        frame_by_angle=frame_by_angle,
        linearization_by_role=linearization_by_role,
        headroom_charge_db_for=headroom_charge_db_for,
        weights=weights,
    )

    incumbent_refusal = refusal_for(incumbent, bounds)
    if incumbent_refusal is not None:
        refusals.append(("incumbent", incumbent_refusal))
    incumbent_score = evaluate(incumbent)
    incumbent_ranked = RankedCandidate(
        candidate=incumbent, score=incumbent_score, domination=None,
    )
    incumbent_point = _incumbent_point(incumbent, plan)

    scored: list[tuple[_GridPoint, XoverCandidate, ObjectiveScore]] = []
    landscape: dict[str, Any] = {"branches": {}}
    winners: dict[str, _GridPoint] = {}
    visited: set[tuple[int, int, float, float, float]] = set()

    def _score(points: Iterator[tuple[_GridPoint, XoverCandidate]]) -> None:
        for point, candidate in points:
            key = (
                point.order, point.polarity, point.fc_hz,
                point.delay_us, point.gain_db,
            )
            if key in visited:
                # The refinement cell overlaps the coarse grid it was centred
                # on, so the winner itself and its neighbours recur. Skipping
                # them keeps the evaluation count honest and keeps a duplicate
                # out of the ranking, where two identical entries would read as
                # two candidates.
                continue
            visited.add(key)
            refusal = refusal_for(candidate, bounds)
            if refusal is not None:
                refusals.append((_point_label(point), refusal))
                continue
            score = evaluate(candidate)
            if score.total is None:
                refusals.append((_point_label(point), "primary_view_not_evaluable"))
                continue
            scored.append((point, candidate, score))
            branch = f"order{point.order}/pol{point.polarity:+d}"
            best = landscape["branches"].get(branch)
            if best is None or score.total < best["total"]:
                landscape["branches"][branch] = {
                    "total": float(score.total),
                    "fc_hz": point.fc_hz,
                    "delay_us": point.delay_us,
                    "gain_db": point.gain_db,
                }
                winners[branch] = point

    _score(_walk(bounds, plan, incumbent, incumbent_point))
    landscape["coarse_evaluated"] = len(scored)
    for branch in sorted(winners):
        _score(_refine(winners[branch], bounds, plan, incumbent))

    ordered = _rank(scored, incumbent_point)
    incumbent_primary = incumbent_score.primary
    ranked = tuple(
        RankedCandidate(
            candidate=candidate,
            score=score,
            domination=_domination(score, incumbent_score),
        )
        for _point, candidate, score in ordered[: max(0, plan.shortlist_size)]
    )
    landscape["evaluated"] = len(scored)
    landscape["incumbent_total"] = incumbent_score.total
    landscape["incumbent_residual_db"] = (
        None if incumbent_primary is None else incumbent_primary.residual_db
    )
    return Shortlist(
        ranked=ranked,
        incumbent=incumbent_ranked,
        bounds=bounds,
        authority=authority,
        landscape=landscape,
        refusals=tuple(refusals),
        evaluated=len(scored),
    )


def _domination(
    score: ObjectiveScore, incumbent: ObjectiveScore,
) -> Domination | None:
    """This candidate against the incumbent, on the primary view's profile."""
    primary, incumbent_primary = score.primary, incumbent.primary
    if primary is None or incumbent_primary is None:
        return None
    return dominates(
        primary.profile, incumbent_primary.profile,
        candidate_residual_db=primary.residual_db,
        incumbent_residual_db=incumbent_primary.residual_db,
    )


def _point_label(point: _GridPoint) -> str:
    """One grid point as a short, stable string for a refusal record."""
    return (
        f"fc={point.fc_hz:.1f}Hz order={point.order} pol={point.polarity:+d} "
        f"delay={point.delay_us:.1f}us gain={point.gain_db:+.2f}dB"
    )


def _incumbent_point(incumbent: XoverCandidate, plan: SearchPlan) -> _GridPoint:
    """The incumbent expressed in the grid's own coordinates.

    A candidate with no corner at all takes ``0.0``, which makes every octave
    distance from it degenerate — handled in :func:`_incumbent_distance` rather
    than by inventing a corner the speaker is not running.
    """
    corners = incumbent.corner_hz
    return _GridPoint(
        fc_hz=float(corners[0]) if corners else 0.0,
        order=int(next(
            (
                section.order
                for section in incumbent.sections_by_role.get(plan.hf_role, ())
            ),
            4,
        )),
        polarity=int(incumbent.polarity_by_role.get(plan.hf_role, 1)),
        delay_us=float(incumbent.delay_us_by_role.get(plan.hf_role, 0.0)),
        gain_db=float(incumbent.gain_db_by_role.get(plan.hf_role, 0.0)),
    )


def _walk(
    bounds: CandidateBounds,
    plan: SearchPlan,
    incumbent: XoverCandidate,
    incumbent_point: _GridPoint,
) -> Iterator[tuple[_GridPoint, XoverCandidate]]:
    """Every grid point the coarse pass visits, in a fixed order.

    Sorted on every axis so two runs walk identically. No refinement here: the
    coarse pass is what produces the landscape, and refining before the whole
    landscape exists is the local-minimum trap the module docstring rejects.
    """
    if bounds.fc_band_hz is None:
        return
    corners = _geometric_steps(bounds.fc_band_hz, plan.fc_steps)
    delays = _lattice_steps(
        bounds.delay_window_us, bounds.delay_step_us, plan.delay_steps,
    )
    gains = _gain_steps(
        incumbent_point.gain_db, plan.gain_window_db, plan.gain_steps,
        bounds.gain_db_range,
    )
    for order in sorted(bounds.legal_orders):
        for polarity in (1, -1):
            for fc_hz in corners:
                for delay_us in delays:
                    for gain_db in gains:
                        point = _GridPoint(
                            fc_hz=fc_hz, order=order, polarity=polarity,
                            delay_us=delay_us, gain_db=gain_db,
                        )
                        yield point, _candidate_at(
                            fc_hz=fc_hz, order=order, polarity=polarity,
                            delay_us=delay_us, gain_db=gain_db,
                            plan=plan, incumbent=incumbent,
                        )


def _refine(
    winner: _GridPoint,
    bounds: CandidateBounds,
    plan: SearchPlan,
    incumbent: XoverCandidate,
) -> Iterator[tuple[_GridPoint, XoverCandidate]]:
    """The finer grid inside one coarse cell around ``winner``.

    Each axis refines in ITS OWN units, and one of them refuses to:

    * **Fc** is geometric, so the cell is one coarse RATIO either side and the
      finer steps are geometric inside it.
    * **gain** is linear, so the cell is one coarse step either side at
      ``1/refinement`` of that step.
    * **delay does not refine below the chain's lattice.** The coarse pass may
      have thinned the realizable points to fit its ceiling; refinement restores
      every lattice point inside the cell and stops there. A finer delay is a
      number the emitted graph rounds away, so proposing one would be
      resolution the search does not have — and
      :func:`~jasper.active_speaker.crossover_v2.candidate_space.refusal_for`
      would refuse it by name anyway.

    Order and polarity are already exhaustive, so they do not appear: there is
    nothing between LR4 and LR8 and nothing between ``+1`` and ``-1``.
    """
    if bounds.fc_band_hz is None or plan.refinement < 1:
        return
    lo_hz, hi_hz = bounds.fc_band_hz
    coarse = _geometric_steps(bounds.fc_band_hz, plan.fc_steps)
    ratio = (coarse[1] / coarse[0]) if len(coarse) > 1 else 1.0
    corners = tuple(
        value for value in _geometric_steps(
            (
                max(float(lo_hz), winner.fc_hz / ratio),
                min(float(hi_hz), winner.fc_hz * ratio),
            ),
            2 * plan.refinement + 1,
        )
    ) or (winner.fc_hz,)

    coarse_delays = _lattice_steps(
        bounds.delay_window_us, bounds.delay_step_us, plan.delay_steps,
    )
    cell_us = (
        abs(coarse_delays[1] - coarse_delays[0]) if len(coarse_delays) > 1
        else abs(bounds.delay_step_us)
    )
    delays = _lattice_steps(
        (
            max(float(bounds.delay_window_us[0]), winner.delay_us - cell_us),
            min(float(bounds.delay_window_us[1]), winner.delay_us + cell_us),
        ),
        bounds.delay_step_us,
        2 * plan.refinement + 1,
    ) or (winner.delay_us,)

    gain_cell_db = (
        2.0 * plan.gain_window_db / (plan.gain_steps - 1) if plan.gain_steps > 1
        else GAIN_SEARCH_STEP_DB
    )
    gains = _gain_steps(
        winner.gain_db, gain_cell_db, 2 * plan.refinement + 1,
        bounds.gain_db_range,
    ) or (winner.gain_db,)

    for fc_hz in corners:
        for delay_us in delays:
            for gain_db in gains:
                point = _GridPoint(
                    fc_hz=fc_hz, order=winner.order, polarity=winner.polarity,
                    delay_us=delay_us, gain_db=gain_db,
                )
                yield point, _candidate_at(
                    fc_hz=fc_hz, order=winner.order, polarity=winner.polarity,
                    delay_us=delay_us, gain_db=gain_db,
                    plan=plan, incumbent=incumbent,
                )


def _rank(
    scored: Sequence[tuple[_GridPoint, XoverCandidate, ObjectiveScore]],
    incumbent_point: _GridPoint,
) -> list[tuple[_GridPoint, XoverCandidate, ObjectiveScore]]:
    """Sort by score, then resolve the flat minimum toward the incumbent.

    Two passes rather than one comparator: the epsilon is measured from the BEST
    score, which is not known until everything is scored. Inside the flat region
    the order is by distance from the incumbent; outside it the order is by
    score. A point exactly at the epsilon boundary is inside it — the boundary
    belongs to "these are the same", which is the direction that prefers the
    smaller change.
    """
    if not scored:
        return []
    ordered = sorted(
        scored,
        key=lambda entry: (
            entry[2].total if entry[2].total is not None else math.inf,
            _incumbent_distance(entry[0], incumbent_point),
        ),
    )
    best = ordered[0][2].total
    if best is None:
        return ordered
    flat = [
        entry for entry in ordered
        if entry[2].total is not None
        and entry[2].total - best <= FLAT_MINIMUM_EPSILON_DB
    ]
    flat.sort(key=lambda entry: _incumbent_distance(entry[0], incumbent_point))
    return flat + ordered[len(flat):]


def _evaluator(
    *,
    plants: Sequence[DriverPlant],
    role_by_angle: Mapping[float, str],
    primary_role: str,
    on_axis_deg: float,
    frame_by_angle: Mapping[float, GradingFrame],
    linearization_by_role: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    headroom_charge_db_for: Any,
    weights: ObjectiveWeights,
) -> Any:
    """A ``candidate -> ObjectiveScore`` closure over everything held fixed.

    Built once so the fixed half — plants, frames, roles, weights — is bound
    once rather than threaded through every grid point, and so the search body
    reads as a walk rather than as an argument list.
    """

    def evaluate(candidate: XoverCandidate) -> ObjectiveScore:
        predicted = predict_sum(
            candidate, plants, linearization_by_role=linearization_by_role,
        )
        magnitudes = _magnitudes_by_angle(predicted)
        grid = next(
            (plant.freqs_hz for plant in plants if float(plant.angle_deg) in magnitudes),
            None,
        )
        region_hz = _crossover_region_hz(candidate)
        return score_prediction(
            magnitudes,
            np.asarray([] if grid is None else grid, dtype=np.float64),
            role_by_angle=role_by_angle,
            primary_role=primary_role,
            on_axis_deg=on_axis_deg,
            frame_by_angle=frame_by_angle,
            crossover_region_hz=region_hz,
            headroom_charge_db=(
                0.0 if headroom_charge_db_for is None
                else float(headroom_charge_db_for(candidate))
            ),
            weights=weights,
        )

    return evaluate


def _crossover_region_hz(candidate: XoverCandidate) -> tuple[float, float]:
    """The octave either side of the corner — where the branches overlap.

    One octave because that is where two Linkwitz-Riley branches are both still
    contributing: an LR4 is 24 dB down an octave out, which is the point past
    which the other branch owns the sum and a directivity kink can no longer be
    the handoff's fault. A candidate with no corner gets a degenerate region and
    :func:`~jasper.active_speaker.crossover_v2.objective.directivity_continuity`
    reports it as unevaluable rather than measuring a region nothing crosses in.
    """
    corners = candidate.corner_hz
    if not corners:
        return (0.0, 0.0)
    return (float(corners[0]) / 2.0, float(corners[-1]) * 2.0)


def _refusals_by_reason(
    refusals: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Every refusal reason, with how many points it refused and one example.

    Grouped rather than listed one per point, because a bound sitting inside the
    search band refuses hundreds of grid points for one reason and a document
    that printed all of them would bury everything else in it. Nothing goes
    silent: every reason that fired is named with its count, which is the fact a
    reader acts on — "the declared floor refused 812 of 2,520 points" sends them
    to the declaration, and 812 rows saying so do not send them anywhere new.
    The complete per-point list stays on :attr:`Shortlist.refusals`.

    Sorted by count descending then by reason, so the loudest wall reads first
    and the order is stable across runs.
    """
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for what, why in refusals:
        counts[why] = counts.get(why, 0) + 1
        examples.setdefault(why, what)
    return [
        {"reason": why, "count": counts[why], "example": examples[why]}
        for why in sorted(counts, key=lambda reason: (-counts[reason], reason))
    ]


def prediction_document(shortlist: Shortlist) -> dict[str, Any]:
    """The shortlist as one document, in the harness's own currency.

    Mirrors the evidence packet's two rules rather than inventing a shape:
    anything not computable is DISCLOSED in ``not_evaluated`` rather than
    omitted, and every refusal is named. Residuals are the same
    ``spec_convergence_residual`` dB the round grades in, so a predicted
    improvement and a measured one are the same quantity.

    ``ranking_authority`` is printed here and not merely documented. While it
    reads :data:`RANKING_AUTHORITY_BRACKET_ONLY`, ``ranked`` names the
    candidates worth measuring and its ORDER carries no claim — a reader acting
    on the order alone would be repeating the mistake this stamp exists to
    prevent.
    """
    incumbent_primary = shortlist.incumbent.score.primary
    return {
        "kind": "crossover_shortlist",
        "ranking_authority": shortlist.authority.to_dict(),
        "currency": {
            "residual": "flat_spec.spec_convergence_residual.rms_db",
            "units": "dB",
            "lower_is_better": True,
            "acceptance": (
                "the primary residual must improve by the material bar AND no "
                "chunk may regress past the chunk bar — see Domination"
            ),
        },
        "bounds": {
            "fc_band_hz": (
                None if shortlist.bounds.fc_band_hz is None
                else list(shortlist.bounds.fc_band_hz)
            ),
            "fc_lo_role": shortlist.bounds.fc_lo_role,
            "fc_hi_role": shortlist.bounds.fc_hi_role,
            "legal_orders": sorted(shortlist.bounds.legal_orders),
            "delay_window_us": list(shortlist.bounds.delay_window_us),
            "delay_step_us": shortlist.bounds.delay_step_us,
            "gain_db_range": list(shortlist.bounds.gain_db_range),
            "beaming_ceiling_hz": shortlist.bounds.beaming_ceiling_hz,
            "directivity_ceiling_hz": shortlist.bounds.directivity_ceiling_hz,
            "matched_beamwidth_hz": shortlist.bounds.matched_beamwidth_hz,
            "undeclared_roles": list(shortlist.bounds.undeclared_roles),
        },
        "incumbent": shortlist.incumbent.to_dict(),
        "incumbent_residual_db": (
            None if incumbent_primary is None else incumbent_primary.residual_db
        ),
        "ranked": [entry.to_dict() for entry in shortlist.ranked],
        "landscape": dict(shortlist.landscape),
        "refusals": _refusals_by_reason(shortlist.refusals),
        "evaluated": shortlist.evaluated,
        "not_evaluated": [
            list(pair)
            for pair in sorted({
                pair
                for ranked in (shortlist.incumbent, *shortlist.ranked)
                for pair in ranked.score.not_evaluated
            })
        ],
        "vertical_polar": (
            "NOT MEASURED. Every view and the directivity term are horizontal "
            "only; a crossover's primary artifact is vertical lobing, which no "
            "pose here witnesses. Silence about it is not evidence."
        ),
    }
