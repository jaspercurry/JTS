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

**WHAT IS NOT WIRED, and exactly what is missing.** Nothing in ``jasper/``
calls :func:`search_candidates` yet, and the reason is one missing input rather
than a missing caller. :func:`~.forward_model.driver_plants` needs measurements
duck-typed on ``role`` / ``angle_deg`` / ``freqs_hz`` / ``complex_tf`` /
``band_hz``, and **no banked artifact carries that object**: ``complex_tf``
appears in no serializer anywhere in the tree, and
:func:`~.round_views.load_banked_round` returns per-POSITION real magnitude
curves with ``degrees=None`` and one COMBINED
:class:`~jasper.active_speaker.flat_spec.FlatSpecReport`. Per-driver complex
transfer functions are built only in a live session's memory
(:class:`~.spatial.LateralPoseCurve`, itself missing ``angle_deg``), and it is
that DERIVED object which is dropped when the session ends — the raw material
it was derived from survives in the bundle.

So the wiring is a capture-side loader rather than an adapter, and it is a
loader the tree can already half-build: deconvolve the banked per-pose WAVs,
which :func:`~.round_views.verify_pose_curve` already does against each
capture's own banked program, then join ``role`` and ``position_deg`` from the
positions sidecars and take the driven band from the sweep segment. Four
smaller inputs ARE derivable from a banked round today and are named here so
the next session does not re-derive them: ``protection_by_role`` via
:func:`~jasper.active_speaker.branch_chain.confirmed_protection_sections`,
which takes the confirmed safety profile AND ``role_targets``, then
:func:`~.priors.role_transfers`; the required-band
narrowing via :func:`~.priors.candidate_required_band_hz`, which is the one
owner and whose whole-band default refused every banked armrun arm;
``delay_step_us`` as ``1e6 / camilla_config_contract.DEFAULT_SAMPLE_RATE``; and
the per-seat frozen reference via :func:`~.round_views.frozen_reference_grade`,
which keys by ``position_id`` rather than by angle.

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
    "DELAY_RANKING_MARK_RHO",
    "DELAY_RANKING_MEASURED_RHO",
    "FLAT_MINIMUM_EPSILON_DB",
    "GAIN_SEARCH_STEP_DB",
    "GAIN_SEARCH_WINDOW_DB",
    "MARK_VS_POOL_REFEREE_RHO",
    "RANKING_AUTHORITY_BRACKET_ONLY",
    "RANKING_AUTHORITY_RANK",
    "REFINEMENT_FACTOR",
    "SHORTLIST_SCHEMA_VERSION",
    "Bracket",
    "RankedCandidate",
    "RankingAuthority",
    "SearchPlan",
    "Shortlist",
    "ShortlistProvenance",
    "prediction_document",
    "search_candidates",
    "spearman_rho",
]

#: The shape of :func:`prediction_document`'s output.
#:
#: A plain int and a module constant, the same shape
#: :data:`~jasper.active_speaker.crossover_v2.evidence_packet.PACKET_SCHEMA_VERSION`
#: takes, so a reader that already knows one artifact's versioning convention
#: knows this one's. Bump when a consumer would have to change to keep reading
#: the document.
SHORTLIST_SCHEMA_VERSION = 1

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
#: artifact reports :data:`DELAY_RANKING_MARK_RHO`, which would CLEAR the bar
#: above. That is not a reason to adopt the mark: the same analysis swings to
#: **-1.0000** on the frozen reference over the invert-only subset, and the
#: artifact's own verdict on it is that "a statistic that swings from +0.66 to
#: -1.00 across reasonable analysis choices is not evidence in either
#: direction — at the mark, this bank cannot answer the question." The pooled
#: axis is taken because it is the campaign's own referee and it is stable, and
#: the mark figure is recorded beside this one so nobody rediscovers it later
#: and reads a passing grade off it. The two measured axes anti-correlate with
#: each other at :data:`MARK_VS_POOL_REFEREE_RHO`, which is the lobe re-aiming
#: this module's directivity term exists to charge for.
DELAY_RANKING_MEASURED_RHO: float = -1.0

#: The same lever against the ON-AXIS MARK referee — the EXCULPATORY number.
#:
#: The figure a reader reaches for to argue the guard above is too harsh, so it
#: is a named constant rather than prose: an argument made from a number nobody
#: can check is an argument nobody can refute. Self-referenced across all six
#: arms, and on its own it would clear :data:`DELAY_RANKING_BAR_RHO`.
#:
#: It does not rescue the pooled verdict, and the reason is arithmetic rather
#: than preference — see :data:`MARK_VS_POOL_REFEREE_RHO`.
DELAY_RANKING_MARK_RHO: float = 0.6571

#: What the two measured REFEREES say about each other, on the same six arms.
#:
#: The on-axis mark and the pooled five-position measurement anti-correlate:
#: this is the lobe being re-aimed — on-axis flatness bought at off-axis cost —
#: and it is the defect :func:`~jasper.active_speaker.crossover_v2.objective.directivity_continuity`
#: exists to charge for.
#:
#: It is also what closes the door on reading a passing grade off
#: :data:`DELAY_RANKING_MARK_RHO`. Three correlations over one set of arms are
#: not free of each other: a correlation matrix must stay positive
#: semi-definite, which bounds the third given two. With the mark figure at
#: +0.6571 and the referees at -0.6571, the pooled rho is confined to
#: [-1.0000, +0.1364] — an interval lying ENTIRELY below the +0.6 bar. So the
#: model cannot have ranking authority against the pooled referee no matter how
#: the mark figure is read, and the recorded -1.000 sits exactly at that
#: interval's lower extreme. Pinned by a test rather than asserted here.
#:
#: That the lower bound lands on -1.0000 EXACTLY rather than near it follows
#: from the two figures being equal in magnitude, which is a coincidence of
#: this bank and not an identity: an untied Spearman over six arms can only
#: take the values ``1 - sum(d^2)/35``, and these two are ``sum(d^2) = 12`` and
#: ``58``, which happen to sit 23 either side of 35. Do not carry the equality
#: forward to another bank.
MARK_VS_POOL_REFEREE_RHO: float = -0.6571

#: Where :data:`DELAY_RANKING_MEASURED_RHO` came from, so the number can be
#: re-derived rather than believed. The same artifact carries
#: :data:`DELAY_RANKING_MARK_RHO` and :data:`MARK_VS_POOL_REFEREE_RHO`.
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
class ShortlistProvenance:
    """WHICH round, capture and speaker a prediction document speaks for.

    Every field is the CALLER's to state, and every one defaults to ``None``.
    This module reads no clock, no filesystem and no hostname — the same
    posture :mod:`.evidence_packet` takes, where no ``datetime.now`` appears at
    all. A module that stamped its own time would make two runs over identical
    inputs produce different documents, which would break the determinism the
    walk is built to guarantee and which a test asserts byte for byte.

    ``generated_at`` is therefore a string the caller passes, expected as
    ISO-8601 UTC. It is not validated here: this module has no opinion about
    the caller's clock, and a parse it could only fail on would trade one
    unverifiable field for one unverifiable exception.

    ``identified`` is derived rather than declared, so a document cannot claim
    to name its round while carrying nothing.
    """

    round_id: str | None = None
    session_id: str | None = None
    speaker_id: str | None = None
    generated_at: str | None = None

    @property
    def identified(self) -> bool:
        """Whether anything at all ties this document to a round."""
        return any(
            value is not None
            for value in (self.round_id, self.session_id, self.speaker_id)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "session_id": self.session_id,
            "speaker_id": self.speaker_id,
            "generated_at": self.generated_at,
            "identified": self.identified,
        }


@dataclass(frozen=True)
class Bracket:
    """WHERE the flat minimum is, and a set of candidates that spans it.

    This is what a model with :data:`RANKING_AUTHORITY_BRACKET_ONLY` is
    actually allowed to say, expressed as the artifact rather than promised in
    prose. The thing it replaces was a top-N "ranked" list, and the top-N was
    wrong on its own terms twice over: the ranking helper it used re-sorted the
    flat region by distance from the incumbent, so the head of the list was the
    SMALLEST CHANGE inside the region and not the walk's minimum; and a small
    ``shortlist_size`` could then truncate the minimum out of the list
    entirely, while ``landscape["branches"]`` went on holding a strictly better
    point the reader never saw. That helper is gone, so this paragraph names it
    by what it did rather than by a symbol a reader would search for in vain.

    ``members`` fixes both by construction. The walk's argmin is always one,
    every branch (order x polarity) that reaches the flat region contributes
    its own best, and so does every distinct corner — so a reader measuring the
    members samples the region's SHAPE instead of one corner of it. Members are
    emitted in incumbent-distance order, which is a PICK order for an operator
    choosing what to measure first, never a ranking: with the H5 stamp at
    bracket-only, position in this tuple carries no claim about the speaker.

    The per-axis extents say how wide the minimum is in each variable the walk
    moved. A 300 Hz-wide fc extent and a 20 us-wide delay extent are different
    messages about what the next measurement should resolve, and a list of
    points does not carry either one.

    **This is provenance, not a gate** (``docs/measurement-loop-doctrine.md``
    section 3: prediction-engine rankings "ride with the data ... [and] must
    never refuse an experiment on [their] own"). A candidate outside these
    extents is not forbidden — it is merely not one the walk found inside the
    flat minimum. Nothing here refuses anything.

    ``guaranteed`` is how many members the diversity rule itself required and
    ``requested_size`` is what the plan asked for. When the first exceeds the
    second the guarantee wins and both numbers are published, because silently
    honouring a size cap by dropping the argmin is the exact failure this
    class replaces.
    """

    members: tuple[RankedCandidate, ...]
    fc_hz_range: tuple[float, float]
    order_set: tuple[int, ...]
    polarity_set: tuple[int, ...]
    delay_us_range: tuple[float, float]
    gain_db_range: tuple[float, float]
    epsilon_db: float
    #: The region's best advisory total, or ``None`` when nothing scored.
    #: Never an infinity — see :func:`_flat_region`.
    best_total: float | None
    n_in_region: int
    guaranteed: int
    requested_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": [entry.to_dict() for entry in self.members],
            "extent": {
                "fc_hz": list(self.fc_hz_range),
                "order": list(self.order_set),
                "polarity": list(self.polarity_set),
                "delay_us": list(self.delay_us_range),
                "gain_db": list(self.gain_db_range),
            },
            "epsilon_db": self.epsilon_db,
            "best_total": self.best_total,
            "n_in_region": self.n_in_region,
            "guaranteed": self.guaranteed,
            "requested_size": self.requested_size,
            "member_order": (
                "distance from the incumbent — a PICK order for an operator "
                "choosing what to measure first, never a ranking"
            ),
            "extent_is_provenance": (
                "a candidate outside these extents is not forbidden; the walk "
                "simply did not find it inside the flat minimum"
            ),
        }


@dataclass(frozen=True)
class Shortlist:
    """The bracket, the incumbent, and everything the walk refused.

    ``incumbent`` is always present and always evaluated — ``select_fc``'s S9.8
    discipline kept verbatim: the configured candidate is in the evaluated set,
    an alternative must beat it, and keeping configured is an honest verdict.
    It is NOT in the bracket unless it earned a place there on its own score.

    ``authority`` is the H5 stamp. While it is
    :data:`RANKING_AUTHORITY_BRACKET_ONLY`, :attr:`Bracket.members` names the
    candidates worth MEASURING and their ORDER is not a claim about the
    speaker.
    """

    bracket: Bracket
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

    The result is a :class:`Bracket` rather than a ranked top-N: the flat
    minimum's per-axis extent, plus members chosen so the walk's argmin, every
    branch in the region and every corner in it are all represented. Distance
    from the incumbent (:func:`_incumbent_distance`) orders the members, so a
    flat minimum still resolves toward the smallest change rather than toward
    whichever grid point numpy visited first — but it orders WITHIN the
    bracket and no longer decides membership, which is what let a truncated
    list lose the minimum.
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

    region, best_total = _flat_region(scored)
    selected, guaranteed = _bracket_members(
        region, incumbent_point, requested_size=plan.shortlist_size,
    )
    extents = _axis_extents(region)
    incumbent_primary = incumbent_score.primary
    bracket = Bracket(
        members=tuple(
            RankedCandidate(
                candidate=candidate,
                score=score,
                domination=_domination(score, incumbent_score),
            )
            for _point, candidate, score in selected
        ),
        epsilon_db=FLAT_MINIMUM_EPSILON_DB,
        best_total=best_total,
        n_in_region=len(region),
        guaranteed=guaranteed,
        requested_size=int(plan.shortlist_size),
        **extents,
    )
    landscape["evaluated"] = len(scored)
    landscape["incumbent_total"] = incumbent_score.total
    landscape["incumbent_residual_db"] = (
        None if incumbent_primary is None else incumbent_primary.residual_db
    )
    return Shortlist(
        bracket=bracket,
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


def _flat_region(
    scored: Sequence[tuple[_GridPoint, XoverCandidate, ObjectiveScore]],
) -> tuple[list[tuple[_GridPoint, XoverCandidate, ObjectiveScore]], float | None]:
    """Every scored point within :data:`FLAT_MINIMUM_EPSILON_DB` of the best.

    The region, not an ordering of it — ordering is a separate question with a
    separate answer, and conflating the two is what let a tie-break masquerade
    as a ranking. A point exactly at the epsilon boundary is INSIDE the region:
    the boundary belongs to "these are the same", which is the direction that
    prefers the smaller change.

    The best total is ``None`` when nothing scored — every candidate refused,
    or no legal band to walk. Not an infinity: this number is published in a
    JSON document, and ``float("inf")`` leaves ``json.dumps`` emitting a bare
    ``Infinity`` token that is not JSON and that a strict parser rejects. The
    package already has one answer for this, and it is
    ``crossover_envelope_v2._finite``'s: a non-finite number becomes ``None``.
    """
    totals = [
        entry[2].total for entry in scored if entry[2].total is not None
    ]
    if not totals:
        return [], None
    best = min(totals)
    return (
        [
            entry for entry in scored
            if entry[2].total is not None
            and entry[2].total - best <= FLAT_MINIMUM_EPSILON_DB
        ],
        float(best),
    )


def _bracket_members(
    region: Sequence[tuple[_GridPoint, XoverCandidate, ObjectiveScore]],
    incumbent_point: _GridPoint,
    *,
    requested_size: int,
) -> tuple[list[tuple[_GridPoint, XoverCandidate, ObjectiveScore]], int]:
    """One point per branch, one per corner — each its group's best — then fill.

    Two grouping rules, each answering a question a single point cannot:

    * **one per branch** (order x polarity), because branches are different
      FILTERS and a region that spans two of them is telling the operator the
      measurement has to decide between shapes, not nudge a number.
    * **one per distinct corner**, because the corner is the axis a crossover
      argument is actually about, and sampling one corner five times at five
      delays would measure the delay axis while the fc extent went untested.

    Each slot takes its own group's BEST point rather than an arbitrary member,
    walking ``by_total``, so the set is a lower envelope of the region rather
    than a scatter through it.

    **The walk's argmin is a member, and the branch rule is why** — stated
    rather than enforced twice. Every point belongs to exactly one branch, so
    the region's best point IS its own branch's best, and the branch rule takes
    each branch's best; an explicit argmin line alongside it would be a second
    guarantee of one fact and could drift from the first. That membership is
    the concrete defect the top-N had: with the flat region re-sorted toward
    the incumbent and a small size cap, the minimum fell off the end of the
    list while ``landscape["branches"]`` still held it. A test pins the
    membership against ``landscape``, so the guarantee is checked at the
    property rather than at the line that happens to deliver it.

    ``requested_size`` then fills from what is left, nearest the incumbent
    first. It is a target and not a ceiling: when the three guarantees already
    exceed it they win, and the caller is told so through
    :attr:`Bracket.guaranteed`.
    """
    if not region:
        return [], 0
    by_total = sorted(
        region,
        key=lambda entry: (
            entry[2].total if entry[2].total is not None else math.inf,
            _incumbent_distance(entry[0], incumbent_point),
        ),
    )
    chosen: dict[tuple[int, int, float, float, float], Any] = {}

    def _take(entry: tuple[_GridPoint, XoverCandidate, ObjectiveScore]) -> None:
        point = entry[0]
        chosen.setdefault(
            (point.order, point.polarity, point.fc_hz, point.delay_us, point.gain_db),
            entry,
        )

    for group in (
        lambda point: (point.order, point.polarity),
        lambda point: point.fc_hz,
    ):
        seen: set[Any] = set()
        for entry in by_total:
            key = group(entry[0])
            if key in seen:
                continue
            seen.add(key)
            _take(entry)
    guaranteed = len(chosen)

    for entry in sorted(
        region, key=lambda entry: _incumbent_distance(entry[0], incumbent_point),
    ):
        if len(chosen) >= max(0, requested_size):
            break
        _take(entry)

    members = sorted(
        chosen.values(),
        key=lambda entry: _incumbent_distance(entry[0], incumbent_point),
    )
    return members, guaranteed


def _axis_extents(
    region: Sequence[tuple[_GridPoint, XoverCandidate, ObjectiveScore]],
) -> dict[str, Any]:
    """How wide the flat minimum is on each axis the walk moved."""
    points = [entry[0] for entry in region]
    if not points:
        return {
            "fc_hz_range": (0.0, 0.0),
            "order_set": (),
            "polarity_set": (),
            "delay_us_range": (0.0, 0.0),
            "gain_db_range": (0.0, 0.0),
        }
    return {
        "fc_hz_range": (
            min(point.fc_hz for point in points),
            max(point.fc_hz for point in points),
        ),
        "order_set": tuple(sorted({point.order for point in points})),
        "polarity_set": tuple(sorted({point.polarity for point in points})),
        "delay_us_range": (
            min(point.delay_us for point in points),
            max(point.delay_us for point in points),
        ),
        "gain_db_range": (
            min(point.gain_db for point in points),
            max(point.gain_db for point in points),
        ),
    }


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


def prediction_document(
    shortlist: Shortlist, *, provenance: ShortlistProvenance | None = None,
) -> dict[str, Any]:
    """The shortlist as one document, in the harness's own currency.

    Mirrors the evidence packet's two rules rather than inventing a shape:
    anything not computable is DISCLOSED in ``not_evaluated`` rather than
    omitted, and every refusal is named. Residuals are the same
    ``spec_convergence_residual`` dB the round grades in, so a predicted
    improvement and a measured one are the same quantity.

    ``ranking_authority`` is printed here and not merely documented. While it
    reads :data:`RANKING_AUTHORITY_BRACKET_ONLY`, ``bracket.members`` names the
    candidates worth measuring and their ORDER carries no claim — a reader
    acting on the order alone would be repeating the mistake this stamp exists
    to prevent.

    ``provenance`` says WHICH round this document speaks for. Absent, the
    block is still emitted with null fields and
    ``"identified": false``, because a shortlist that cannot name its round is
    a fact a reader needs rather than one to hide behind a missing key: two
    documents from two captures are otherwise indistinguishable once they
    leave the process that made them.
    """
    incumbent_primary = shortlist.incumbent.score.primary
    stated = provenance or ShortlistProvenance()
    return {
        "kind": "crossover_shortlist",
        "schema_version": SHORTLIST_SCHEMA_VERSION,
        "provenance": stated.to_dict(),
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
            # Both halves of the floor wall. ``below_declared_floor`` has two
            # causes — a corner under a declared floor, or a floor nothing
            # high-passes at all — and the block published neither number, so
            # the artifact could not tell a reader which one a walk met. #2760
            # is what that cost: the refusal read as a bad corner for as long
            # as it took to find the missing filter.
            "declared_floor_hz_by_role": dict(
                shortlist.bounds.declared_floor_hz_by_role
            ),
            "standing_highpass_hz_by_role": dict(
                shortlist.bounds.standing_highpass_hz_by_role
            ),
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
        "bracket": shortlist.bracket.to_dict(),
        "landscape": dict(shortlist.landscape),
        "refusals": _refusals_by_reason(shortlist.refusals),
        "evaluated": shortlist.evaluated,
        "not_evaluated": [
            list(pair)
            for pair in sorted({
                pair
                for entry in (shortlist.incumbent, *shortlist.bracket.members)
                for pair in entry.score.not_evaluated
            })
        ],
        "vertical_polar": (
            "NOT MEASURED. Every view and the directivity term are horizontal "
            "only; a crossover's primary artifact is vertical lobing, which no "
            "pose here witnesses. Silence about it is not evidence."
        ),
    }
