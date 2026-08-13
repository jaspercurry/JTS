# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WHERE to cross, and what it costs to find out (#2291 Phase 5a-v(b)).

A sibling of :mod:`.programs`, :mod:`.priors`, :mod:`.spatial` and
:mod:`.candidates`.  Those answer what a phase plays, what the analyzer is told,
what a capture-consuming phase decides, and what one build produced.  This one
answers R17's question: **given a capture that is alive exactly once, which
crossover corners may this speaker be asked about, what does each one cost to
score, and which one does the evidence recommend?**

**Why it could not move until now.**  ``evaluate_candidate`` consumes a build
product, and through it the linearization state and the cloud terms — which is
why 5a-iii deferred the sweep with "its return types are 5a-v territory" and
why 5a-v(a) moved :mod:`.candidates` first.  With the values already here, the
machinery that reads them follows without a second flow-local type appearing
behind it.

**Ports, not patches — and the reason is a defect class, not a test detail.**
The seams this module reaches that are *behaviour the host owns* — the
per-candidate evaluation and build, the analyzer, the candidate set, the wall
budget, and the selector kernel — are injected as callables
(:func:`sweep_candidates`'s ``evaluate`` / ``candidate_set_of`` / ``budget_s_of``,
:func:`evaluate_candidate`'s ``analyze`` / ``build``, and :func:`adjudicate`'s
``select``) rather than imported here, so the conductor's own attribute stays
the dispatch point.  That keeps a class-level substitution of
``CrossoverV2Session._evaluate_fc_candidate`` — or of the flow module's
``select_fc`` — binding on production instead of silently addressing a name
production no longer routes through (issue #2354).  A seam nothing substitutes
is called directly, and the conductor's delegate calls the same function, so
there is exactly one derivation either way.

The two ``_of`` suffixes are not decoration: ``candidate_set`` and the module's
own :func:`candidate_set` would otherwise be the same name in the same scope,
and a parameter shadowing a sibling function is how a later edit calls the
wrong one.

**Read-after-write is a port, read-before is a value.**  ``predicted_spec_report``
and ``sweep_bounds`` are callables even though they look like plain state,
because :func:`evaluate_candidate` reads them AFTER its build has run and the
build *writes* the spec report.  Captured as values beforehand, a candidate
would file the previous candidate's report beside its own proposal — precisely
the cross-context leak #2291 exists to close, and the one the 2026-08-10
incident is a member of.  Anything read before the build travels as a value.

**Disclosure goes back through the host, at the moment it happens.**  Every log
line this module would emit is handed to a ``journal`` port as a
:class:`~jasper.active_speaker.crossover_v2.accountability.GateRecord`, synchronously,
where the pre-extraction method logged it.  Returning the records instead would
have re-ordered them: the sweep's per-candidate refusal line is interleaved with
the accountability lines a build emits, so a batch flush at the end would move
it past them.  ``GateRecord`` rather than
:class:`~jasper.active_speaker.crossover_v2.intervention.JournalRecord` for the
reason that type's own docstring gives — the planner's record detaches its
payload and rewrites a tuple as a list, and these payloads' logfmt bytes are the
contract.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import numpy as np

from jasper.active_speaker.fc_selector import (
    EVAL_REFUSED_BUDGET,
    EVAL_REFUSED_UNFITTABLE,
    FcCandidateEvaluation,
    FcSelection,
    fc_comparison_complete,
)
from jasper.active_speaker.linearization_fit import (
    linearization_filters_by_role,
    worst_headroom_cost_db,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    MeasurementPriors,
    overlap_band_hz,
    summed_model_residual_delay_us,
)

from ..branch_chain import CrossoverSection, chain_response, crossover_response_complex, sections_by_role
from ..camilla_yaml import role_polarity
from . import priors as _priors
from .accountability import GateRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .candidates import SpeculativeClose

__all__ = [
    "EVENT_CANDIDATE_REFUSED",
    "EVENT_SELECTION",
    "EVENT_SWEEP",
    "EVENT_SWEEP_REFUSED",
    "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
    "FC_REJECT_AT_OR_BELOW_FLOOR",
    "FC_REJECT_BEAMING",
    "FC_REJECT_OUTSIDE_SEARCH_BAND",
    "FC_SWEEP_COMPUTE_BUDGET_S",
    "MAX_PROPOSED_FC_CANDIDATES",
    "Adjudication",
    "FcCandidateSet",
    "FcSearchBand",
    "adjudicate",
    "branch_operators",
    "candidate_sections",
    "candidate_set",
    "evaluate_candidate",
    "fc_candidate_set",
    "refusal",
    "resolve_fc_search_band",
    "sweep_candidates",
]

#: The four event names this module discloses through its ``journal`` port.
#: Named constants rather than literals for the reason :mod:`.accountability`
#: gives: a journal name is a grep contract — the wiring suite and the field
#: runbooks both match on them — so a rename should be visible as one.
EVENT_SWEEP = "correction.crossover_v2_fc_sweep"
EVENT_SWEEP_REFUSED = "correction.crossover_v2_fc_sweep_refused"
EVENT_CANDIDATE_REFUSED = "correction.crossover_v2_fc_candidate_refused"
EVENT_SELECTION = "correction.crossover_v2_fc_selection"

# How many Fc values the selector may PROPOSE besides the configured one. The
# ratified direction is "at most five safe candidates" (#1894); this is the
# proposed half of that, so the evaluated set is at most six.
MAX_PROPOSED_FC_CANDIDATES = 5

# Why each Fc was refused. Named codes, never a bare number, because every one
# of these is a household- or operator-actionable declaration rather than an
# internal detail.
FC_REJECT_AT_OR_BELOW_FLOOR = "at_or_below_declared_floor"
FC_REJECT_ABOVE_LOWER_DRIVER_BAND = "above_lower_driver_band"
FC_REJECT_BEAMING = "beaming_above_ka_ceiling"
FC_REJECT_OUTSIDE_SEARCH_BAND = "outside_declared_search_band"

# Half a display digit: the grid rounds proposals to 0.1 Hz, so a search-band
# edge comparison must not refuse a value it just rounded onto that edge.
_FC_GRID_EPS_HZ = 0.05

#: One-time serial candidate-sweep wall budget. The capture page separately
#: owns a 90 s end-to-end result wait, leaving 20 s for anchor analysis, result
#: publication, polling, and loaded-Pi variance. Post-P0.1 live-Pi all-six
#: timing is unverified; this is the bounded deployment ceiling.
FC_SWEEP_COMPUTE_BUDGET_S = 70.0

#: The exceptions a candidate evaluation may fail with and still leave the
#: capture acceptable. An enumeration rather than a blind ``except Exception``
#: (ruff BLE, and the repository's frozen broad-except budget).
#: ``CrossoverV2FlowError`` is a ``RuntimeError``, so a refused candidate-set
#: derivation lands here rather than escaping into the capture's accept path.
#:
#: ``OSError`` is in the set and the pre-extraction tuple did not have it — a
#: real gap, not a transcription slip. The ``journal`` port is a *logging*
#: delegate, and a logging handler with nowhere to write raises ``OSError``
#: (the ENOSPC shape). Without it, a full disk turns the one advisory that is
#: supposed to be unable to cost a household anything into the thing that
#: fails their completed MEASURE. Both sibling port guards in this package
#: already carry it — ``intervention._PORT_ERRORS`` and
#: ``coordinator._SEAM_ERRORS`` — and this is the third of them.
_SWEEP_ERRORS = (
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
# R17 Fc candidate set (plan §4.2 / #1894 / #1675)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FcCandidateSet:
    """The Fc values the selector may evaluate, and why the others are out.

    ``configured_hz`` is ALWAYS in ``candidates`` (plan §9.8: the configured
    path stays the golden mode until a multi-candidate path proves equivalence
    and then improvement), even when it would fail a bound below. That is not
    an exemption from safety — the declared floor and the lower driver's band
    are hard, and a configured Fc outside THOSE is a broken declaration the
    session refuses long before here. It is an exemption from the BEAMING
    prior, which #1675 defines as guidance to warn on, not a fence.

    ``limits`` carries the derived bounds for disclosure, so a household or an
    operator can see what the search was allowed to consider rather than being
    handed a verdict with no visible reasoning.
    """

    configured_hz: float
    candidates: tuple[float, ...]
    rejected: tuple[tuple[float, str], ...]
    limits: dict[str, float]

    @property
    def alternatives(self) -> tuple[float, ...]:
        """Everything the selector could move TO — the set minus configured."""
        return tuple(fc for fc in self.candidates if fc != self.configured_hz)


def fc_candidate_set(
    *,
    configured_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
    search_band_hz: tuple[float, float] | None = None,
    lower_driver_diameter_mm: float | None = None,
    count: int = MAX_PROPOSED_FC_CANDIDATES,
) -> FcCandidateSet:
    """Derive the bounded LR4 Fc candidate set from DECLARATIONS only.

    Four bounds, each traceable to something a person confirmed:

    * **strictly above** ``hf_hard_floor_hz`` — the operator-confirmed minimum
      for the HF driver. Strict because #1654 measured the edge case: at an Fc
      equal to the floor the candidate's own handoff lands exactly on the
      evidence band's edge, so it cannot be scored honestly even though the
      sweep now reaches it;
    * at or below the lower driver's declared hard ceiling;
    * inside the declared search band when one is declared;
    * at or below the **beaming ceiling** from the lower driver's declared
      diameter (:func:`~jasper.active_speaker.branch_chain.beaming_onset_hz`)
      — a PROPOSAL bound only, per the paragraph in :class:`FcCandidateSet`.

    Proposals are spaced geometrically, because a crossover argument is a
    per-octave one and an arithmetic grid would crowd the top of the range.

    Returns an empty ``candidates`` only when the configured value itself is
    inadmissible; the caller turns that into the ordinary
    ``no_admissible_candidate`` refusal rather than guessing a crossover.
    """
    from jasper.active_speaker.branch_chain import beaming_onset_hz

    lo = float(hf_hard_floor_hz)
    hi = float(lower_driver_hard_ceiling_hz)
    limits: dict[str, float] = {
        "declared_floor_hz": lo,
        "lower_driver_ceiling_hz": hi,
    }
    if search_band_hz is not None:
        limits["search_lo_hz"], limits["search_hi_hz"] = (
            float(search_band_hz[0]), float(search_band_hz[1]),
        )
        hi = min(hi, float(search_band_hz[1]))
        lo = max(lo, float(search_band_hz[0]) - _FC_GRID_EPS_HZ)
    if lower_driver_diameter_mm is not None:
        ceiling = beaming_onset_hz(float(lower_driver_diameter_mm))
        limits["beaming_ceiling_hz"] = ceiling
        hi = min(hi, ceiling)

    rejected: list[tuple[float, str]] = []
    proposed: list[float] = []
    if hi > lo and count > 0:
        # Geometric interior points, excluding both ends: the floor is refused
        # by the strictness rule above and the ceiling is a bound rather than a
        # recommendation, so neither is a value to propose.
        step = (hi / lo) ** (1.0 / (int(count) + 1))
        proposed = [round(lo * step ** (i + 1), 1) for i in range(int(count))]

    candidates = [float(configured_hz)]
    for fc in proposed:
        reason = _fc_rejection(
            fc, hf_hard_floor_hz, lower_driver_hard_ceiling_hz,
            search_band_hz, limits.get("beaming_ceiling_hz"),
        )
        if reason is None:
            candidates.append(fc)
        else:
            rejected.append((fc, reason))
    return FcCandidateSet(
        configured_hz=float(configured_hz),
        candidates=(
            float(configured_hz),
            *sorted(set(candidates) - {float(configured_hz)}),
        ),
        rejected=tuple(rejected),
        limits=limits,
    )


def refusal(fc_hz: float, reason: str) -> FcCandidateEvaluation:
    """A candidate that produced no score, carrying WHY — never a silent drop."""
    empty = np.zeros(0, dtype=np.float64)
    return FcCandidateEvaluation(
        fc_hz=float(fc_hz), freqs_hz=empty, branch_operator_by_role={},
        anchor_sum_db=empty, scoring_band_hz=None, refusal=reason,
    )


def _fc_rejection(
    fc_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
    search_band_hz: tuple[float, float] | None,
    beaming_ceiling_hz: float | None,
) -> str | None:
    """The FIRST bound ``fc_hz`` violates, hardest first, or ``None``."""
    if fc_hz <= float(hf_hard_floor_hz):
        return FC_REJECT_AT_OR_BELOW_FLOOR
    if fc_hz > float(lower_driver_hard_ceiling_hz):
        return FC_REJECT_ABOVE_LOWER_DRIVER_BAND
    if search_band_hz is not None and not (
        float(search_band_hz[0]) - _FC_GRID_EPS_HZ
        <= fc_hz
        <= float(search_band_hz[1]) + _FC_GRID_EPS_HZ
    ):
        return FC_REJECT_OUTSIDE_SEARCH_BAND
    if beaming_ceiling_hz is not None and fc_hz > float(beaming_ceiling_hz):
        return FC_REJECT_BEAMING
    return None


@dataclass(frozen=True)
class FcSearchBand:
    """Which declared search band binds the candidate set, and who narrowed it.

    ``band_hz`` is ``None`` when no proposal may be made at all; the caller
    then evaluates the configured Fc alone, which is an honest verdict rather
    than a failure (plan §9.8).

    ``lo_role`` / ``hi_role`` name a role whose own declaration set each
    surviving edge. They are the whole point of this type: with one number per
    edge and no owner, a household that has declared a stale band sees only
    "nothing was proposed" and has nowhere to go. With the owner named, the
    disclosure can say WHICH driver's declaration is the binding one — the
    operator edits that declaration, which is where the fact lives.

    There is deliberately no "do the roles disagree?" boolean. In a two-way the
    intersection is narrower than somebody's declaration almost every time, so
    such a flag would read ``True`` on nearly every session and mean nothing;
    and a flag comparing only the two EDGE OWNERS misses the live jts3 case
    outright, where both roles declare the same upper limit and the
    disagreement is entirely on the lower one. The two role names carry the
    actionable fact without either failure mode.
    """

    band_hz: tuple[float, float] | None
    lo_role: str | None
    hi_role: str | None
    undeclared_roles: tuple[str, ...]


def resolve_fc_search_band(
    declared_band_hz_by_role: Mapping[str, tuple[float, float] | None],
) -> FcSearchBand:
    """Intersect the participating roles' declared crossover search bands.

    **The rule, stated once.** A two-way crossover at ``Fc`` puts BOTH drivers
    at ``Fc`` — the lower driver is low-passed there and the upper driver is
    high-passed there — so an ``Fc`` is only proposable when EVERY participating
    role's declaration admits it. The binding band is therefore the
    intersection, and it is the fail-closed direction: a tweeter's declared low
    limit is an excursion claim, and the cost of honouring a stale one is a
    proposal not made, while the cost of ignoring it is a driver asked to cross
    below what its declaration permits.

    **A participating role with no declared band yields ``None``** — no
    proposal, disclosed via ``undeclared_roles``. ``crossover_search_band_hz``
    is a required declaration (``driver_safety._target_issues`` refuses a
    target without one), so absence here is an anomaly, and the safe reading of
    an anomaly is "this role has told us nothing about where it may be
    crossed", never "this role permits everything".

    **An empty intersection also yields ``None``** with both edge owners still
    named, because "your woofer says at-or-below 1500 and your tweeter says
    at-or-above 2000" is precisely the actionable sentence, and losing the two
    role names to a bare ``None`` would throw it away.

    Not a safety gate on its own: :func:`fc_candidate_set` still applies the
    declared floor, the lower driver's ceiling, and the ka prior on top of
    whatever this returns. This narrows what may be PROPOSED; it never widens
    it, and it has no say over the configured Fc, which is always evaluated.
    """
    lo_hz = -math.inf
    hi_hz = math.inf
    lo_role: str | None = None
    hi_role: str | None = None
    undeclared: list[str] = []
    for role in sorted(declared_band_hz_by_role):
        band = declared_band_hz_by_role[role]
        if band is None:
            undeclared.append(role)
            continue
        role_lo, role_hi = float(band[0]), float(band[1])
        # Strict ">" / "<" keep the FIRST role to set an edge as its owner, so
        # two roles declaring the same limit name the one that sorts first
        # rather than whichever happened to be iterated last. Sorted iteration
        # above is what makes that deterministic; a tie means both roles
        # declared that limit, so either name is equally true.
        if role_lo > lo_hz:
            lo_hz, lo_role = role_lo, role
        if role_hi < hi_hz:
            hi_hz, hi_role = role_hi, role
    if undeclared or lo_role is None or hi_role is None or lo_hz >= hi_hz:
        return FcSearchBand(
            band_hz=None, lo_role=lo_role, hi_role=hi_role,
            undeclared_roles=tuple(undeclared),
        )
    return FcSearchBand(
        band_hz=(lo_hz, hi_hz), lo_role=lo_role, hi_role=hi_role,
        undeclared_roles=(),
    )


def candidate_set(
    *,
    configured_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
    search_band_hz_by_role: Mapping[str, tuple[float, float] | None],
    lower_driver_diameter_mm: float | None,
) -> FcCandidateSet:
    """This session's proposable Fc set, from DECLARATIONS only (R17).

    Four bounds, all already owned elsewhere and merely gathered here: the
    HF role's declared hard floor and the lower role's declared ceiling
    (``roles_bands``, which is what ``resolve_driver_excitation_ceilings``
    confirmed), the intersected declared search band, and the beaming
    ceiling from the lower driver's declared diameter.

    The configured Fc is always in the returned set even when every bound
    would exclude it (§9.8, and on live jts3 the ka ceiling genuinely sits
    below it) — otherwise this speaker would have no golden candidate to
    prove equivalence against.

    **``FcSearchBand.band_hz is None`` means NO PROPOSAL, and this is the
    one place that has to know it.** The same ``None`` reaching
    :func:`fc_candidate_set` as ``search_band_hz`` would mean the opposite
    — "no declared band constrains this" — and the set would then be bounded
    only by the excitation bands, proposing frequencies below the tweeter's
    own declaration. Translated here to ``count=0``, so the refusal rides
    the ordinary machinery and the returned ``limits`` still explain the
    bounds rather than vanishing with the proposals.
    """
    search = resolve_fc_search_band(search_band_hz_by_role)
    return fc_candidate_set(
        configured_hz=configured_hz,
        hf_hard_floor_hz=hf_hard_floor_hz,
        lower_driver_hard_ceiling_hz=lower_driver_hard_ceiling_hz,
        search_band_hz=search.band_hz,
        lower_driver_diameter_mm=lower_driver_diameter_mm,
        count=0 if search.band_hz is None else MAX_PROPOSED_FC_CANDIDATES,
    )


# --------------------------------------------------------------------------- #
# one candidate: its sections, its operators, its evaluation
# --------------------------------------------------------------------------- #


def candidate_sections(
    crossover_regions: Sequence[Any], fc_hz: float,
) -> dict[str, tuple[CrossoverSection, ...]]:
    """The configured branch sections, re-cornered at ``fc_hz``.

    Order, direction and role assignment are the preset's — only the corner
    moves, because R17 adjudicates WHERE to cross, never what shape to
    cross with (topology search is deferred, #1894).
    """
    return {
        role: tuple(replace(section, fc_hz=float(fc_hz)) for section in sections)
        for role, sections in sections_by_role(crossover_regions or ()).items()
    }


def branch_operators(
    freqs: np.ndarray,
    analysis: Any,
    sections: Mapping[str, tuple[CrossoverSection, ...]],
    linearization: Mapping[str, Any],
    trims: Mapping[str, float],
    *,
    preset: Any,
    tweeter_role: str,
    protection_sections_by_role: Mapping[str, Any] | None,
) -> dict[str, np.ndarray]:
    """What this candidate does to one driver's NEUTRAL pose measurement.

    ``sign * (C_c / P) * K * 10**(trim/20)``, plus the alignment's polarity
    and residual delay on the tweeter — §4.2's re-composition cascaded with
    everything :func:`predicted_branch_sum` applies to the linearized pair.
    So ``sum_role M_pose,role * operator_role`` IS this candidate's model at
    that pose, and the kernel's pose sum is one multiply-add.

    Every factor is evaluated by the SAME function the emitted graph is
    built from — :func:`crossover_response_complex` for the crossover,
    :func:`chain_response` for the correction biquads — so this model and
    the speaker can never disagree about what a filter does.
    """
    protection = _priors.role_transfers(protection_sections_by_role) or {}
    polarity = {
        role: -1 if inverted else 1
        for role, inverted in role_polarity(preset).items()
    }
    filters = linearization_filters_by_role(linearization)
    residual_us = summed_model_residual_delay_us(
        analysis.alignment.anchor_delay_us
        if analysis.alignment.status == ALIGNMENT_OK else None,
        analysis.alignment.delay_us,
    )
    operators: dict[str, np.ndarray] = {}
    for role, section in sections.items():
        emitted = np.asarray(protection[role](freqs), dtype=np.complex128)
        configured = np.asarray(
            crossover_response_complex(freqs, section), dtype=np.complex128
        )
        # ``where=`` guards an exact zero only. The CONDITIONING of this
        # ratio is the analysis's gate, not this function's: a candidate
        # whose C/P left the composition policy's window never reached here
        # (it refused as unfittable), and any non-finite bin that survives
        # is dropped by ``band_flatness``'s own finite mask rather than
        # scored. Re-stating the policy's ceiling here would put a second
        # writer on a number that has one.
        operator = (
            polarity.get(role, 1)
            * np.divide(
                configured, emitted,
                out=np.zeros_like(configured), where=emitted != 0,
            )
            * chain_response(filters.get(role, ()), freqs)
            * 10.0 ** (float(trims.get(role, 0.0)) / 20.0)
        )
        if role == tweeter_role:
            operator = (
                operator
                * analysis.alignment.polarity_sign
                * np.exp(-1j * 2.0 * np.pi * freqs * residual_us * 1e-6)
            )
        operators[role] = operator
    return operators


def evaluate_candidate(
    fc_hz: float,
    anchor: Any,
    *,
    analyze: Callable[[MeasurementPriors], Any],
    build: Callable[..., "SpeculativeClose"],
    measure_priors: Callable[[], MeasurementPriors],
    sweep_bounds: Callable[[], tuple[float | None, float | None]],
    predicted_spec_report: Callable[[], Mapping[str, Any] | None],
    ineligible_reason: Callable[[Any], str | None],
    commanded_delta: Callable[[Any, Any], Any],
    configured_fc_hz: float,
    preset: Any,
    tweeter_role: str,
    protection_sections_by_role: Mapping[str, Any] | None,
    grid: np.ndarray,
) -> FcCandidateEvaluation:
    """One candidate, fitted and reduced to its retained record.

    **This function is the release point.** The per-candidate analysis and
    fit — hundreds of megabytes between them — are locals here, so they are
    unreachable the moment it returns and the next candidate's allocation
    never overlaps them. The configured candidate reuses the anchor's own
    analysis rather than re-running it: it is the same priors, so a second
    run would buy an identical result for 7 s and 400 MB.

    ``predicted_spec_report`` and ``sweep_bounds`` are callables, not values,
    for the reason the module docstring gives: both are read AFTER ``build``
    has run, and ``build`` writes the spec report. A value captured at the
    call would be the PREVIOUS candidate's.
    """
    sections = candidate_sections(getattr(preset, "crossover_regions", ()), fc_hz)
    analysis = (
        anchor if fc_hz == configured_fc_hz
        else analyze(_priors.candidate_priors(measure_priors(), fc_hz, sections))
    )
    cand = analysis.candidate
    # The SAME hard gate ``_build_candidate`` applies, and applied for the
    # same reason: ``plan_linearization`` states in its own docstring that
    # it assumes eligibility and does not re-check, so calling it on a
    # phone-tier or under-repeated session raises inside the fit engine
    # rather than declining. An ineligible session simply has no linearized
    # model to compare candidates with — an honest refusal, never a
    # fallback to the raw prediction, which would put candidates fitted
    # differently side by side and call the difference a crossover.
    if cand is None or ineligible_reason(analysis) is not None:
        return refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
    candidate_preset = replace(preset, crossover_regions=tuple(
        replace(region,
                id=f"{region.lower_driver}_{region.upper_driver}_{round(fc_hz):.0f}hz",
                fc_hz=float(fc_hz))
        for region in preset.crossover_regions))
    built = build(
        analysis, None, candidate_sections=sections, source_preset=candidate_preset,
    )
    candidate = built.candidate
    if candidate.linearization_outcome == "fit_failed":
        return refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
    trims, linearization = candidate.role_attenuations_db, candidate.linearization
    tweeter_lo, woofer_hi = sweep_bounds()
    # THIS candidate's own linearized prediction, off the state its own
    # build returned. Until #2291 Phase 2b it was read back off a conductor
    # field that had to be nulled beforehand and restored afterwards; a
    # value cannot be another candidate's.
    anchor_sum = built.linearization.linearized_predicted_sum
    if anchor_sum is None:
        # Eligible but the fit still produced no linearized prediction.
        return refusal(fc_hz, EVAL_REFUSED_UNFITTABLE)
    spec_report = predicted_spec_report()
    return FcCandidateEvaluation(
        fc_hz=float(fc_hz),
        freqs_hz=grid,
        branch_operator_by_role=branch_operators(
            grid, analysis, sections, linearization, trims,
            preset=preset, tweeter_role=tweeter_role,
            protection_sections_by_role=protection_sections_by_role,
        ),
        anchor_sum_db=np.interp(grid, anchor_sum[0], anchor_sum[1]),
        # ``overlap_band_hz`` with the REAL sweep bounds, never
        # ``crossover_region_band_hz``: that one is built for summed
        # CAPTURES and takes a gate-derived floor, while this scores a
        # per-branch MODEL on the grid the poses share.
        scoring_band_hz=overlap_band_hz(
            float(fc_hz),
            tweeter_sweep_lo_hz=tweeter_lo,
            woofer_sweep_hi_hz=woofer_hi,
        ),
        # The canonical reducer, not a second spelling of it (#2357 item 1).
        # ``worst_headroom_cost_db`` documents itself as the one owner of this
        # figure and takes exactly this input — a persisted
        # ``{role: LinearizationFit.to_dict()}`` mapping — so its ``isfinite``
        # guard belongs to this read too. An inline ``max`` over the same
        # values disagreed with it on a NON-FINITE cost: ``max`` returns NaN or
        # not depending on which role iterates first, and a NaN that reaches
        # the selector is clamped by ``max(0.0, ...)`` to 0.0, which spends the
        # saturation penalty rather than charging it.
        #
        # That divergence is not reachable from here TODAY, and the unification
        # is stated as a single-owner rule rather than a bug fix: a candidate
        # cannot carry a non-finite cost at all, because both the state freeze
        # and the fingerprint freeze refuse one. Which is the point — if that
        # ever relaxes, the reducer's ``isfinite`` guard is what stands between
        # a NaN and the selector, and it should be THIS reducer's.
        headroom_cost_db=worst_headroom_cost_db(linearization),
        candidate=candidate.to_dict(),
        predicted_sum=(np.asarray(built.predicted_sum[0]).copy(),
                       np.asarray(built.predicted_sum[1]).copy()),
        predicted_spec_report=(dict(spec_report)
            if spec_report is not None else None),
        commanded_delta=commanded_delta(analysis.predicted_sum,
                                        built.predicted_sum),
        level_frame_finding=built.level_frame_finding,
        # THIS candidate's realized inter-driver level, which the sweep can
        # carry since the cutover (#2307 gate note N6): the verdict is a
        # value on the build's own state rather than a conductor field the
        # sweep restores, so a selected alternative corner's proposal now
        # records its OWN evidence instead of an absence.
        realized_branch_level=built.linearization.realized_branch_level,
    )


# --------------------------------------------------------------------------- #
# the sweep, and the one recommendation it becomes
# --------------------------------------------------------------------------- #


def sweep_candidates(
    program: Any,
    result: Any,
    anchor: Any,
    *,
    evaluate: Callable[[float, Any, Any, Any], FcCandidateEvaluation],
    candidate_set_of: Callable[[], FcCandidateSet],
    budget_s_of: Callable[[], float],
    journal: Callable[[GateRecord], None],
    configured_fc_hz: float,
    protection_declared: bool,
) -> tuple[FcCandidateEvaluation, ...]:
    """Evaluate the proposable Fc set against THIS capture, then release.

    Runs at MEASURE-consume because the raw capture is alive only there: the
    retained anchor holds derived ``DriverResponse``s, and §4.2's own
    conditioning policy refuses to un-compose them. Adjudication still
    happens at the walk's close, so nothing publishes early (§4.4).

    **Never raises**, for anything in :data:`_SWEEP_ERRORS` — which includes
    the ``OSError`` a logging port raises with nowhere to write, and which
    covers the port calls as well as the arithmetic, since the disclosure is
    exactly as able to fail as the computation. A sweep that cannot run leaves
    the disclosure short and says so; no household loses a measured capture
    because an advisory could not be computed. What still escapes is a consumer
    raising a *custom* exception class, by the same design :mod:`.intervention`
    states for its own port guard: a consumer with its own hierarchy knows what
    it is doing and can raise a stdlib type or wrap. Nothing here writes an
    emitted filter; a selected executable candidate waits for Sound-owned
    acceptance at Review.

    **Returns the evidence rather than stashing it** (#2291 Phase 5a-v(b)).
    The pre-extraction method assigned ``self._fc_evaluations`` from a
    ``finally``; the caller now assigns what this returns. The one path that
    differs is an escape OUTSIDE the caught set (``MemoryError`` and friends):
    the partial list is then lost rather than stashed. That is named rather
    than hidden, and the thing that makes it unobservable is **the host's
    teardown, not the conductor** — ``correction_crossover_v2``'s catch-all
    cleanup arm posts a terminal host event and PURGES the relay session on any
    such escape, so no later reader of ``_fc_evaluations`` exists to see the
    difference. Saying "the capture fails, so nobody reads it" would credit the
    wrong owner: it holds because that teardown purges, and a change there is
    what would make this delta real.

    ``protection_declared`` is False when no protection map exists, which means
    no §4.2 composition at all, so a candidate's crossover cannot be
    substituted for the emitted one.
    """
    if not protection_declared:
        return ()
    # No save/restore (#2291 Phase 2b). The fit used to write seven
    # conductor fields the walk's own candidate build then read, so the
    # sweep had to snapshot them and put them back — the restore was what
    # made the published candidate byte-identical to a no-selector run.
    # Each build now returns its own ``LinearizationState``, so a
    # swept candidate's values never reach the anchor's and there is
    # nothing left to restore.
    started = time.monotonic()
    slowest_s = 0.0
    evaluations: list[FcCandidateEvaluation] = []
    try:
        # INSIDE the try, with the disclosure log, so "never raises" is
        # structural rather than a claim about which of these happens to be
        # total today. Deriving the candidate set reads household
        # declarations and the budget reads the MEASURE program; both are
        # ordinary sources of a malformed-input raise, and both used to sit
        # outside this block — where an advisory could have cost the
        # household an ACCEPTED MEASURE (resilience lens).
        candidates = candidate_set_of()
        budget_s = budget_s_of()
        for fc_hz in candidates.candidates:
            elapsed = time.monotonic() - started
            # Forecast, not a bare deadline check: a candidate costs about
            # what the slowest one so far did, and STARTING one that cannot
            # finish inside the phone's window is how an advisory becomes a
            # terminal failure. The first candidate always runs — there is
            # nothing to forecast from, and a sweep that scores nothing has
            # no comparison to offer.
            if evaluations and elapsed + slowest_s > budget_s:
                evaluations.extend(
                    refusal(rest, EVAL_REFUSED_BUDGET)
                    for rest in candidates.candidates[len(evaluations):]
                )
                break
            try:
                evaluations.append(evaluate(fc_hz, anchor, program, result))
            except _SWEEP_ERRORS as exc:
                journal(GateRecord(
                    event=EVENT_CANDIDATE_REFUSED,
                    level=logging.WARNING,
                    fields={
                        "fc_hz": round(float(fc_hz), 1),
                        "reason": type(exc).__name__,
                    },
                ))
                evaluations.append(refusal(fc_hz, EVAL_REFUSED_UNFITTABLE))
            slowest_s = max(slowest_s, time.monotonic() - started - elapsed)
        attempted = [
            round(e.fc_hz, 1)
            for e in evaluations if e.refusal != EVAL_REFUSED_BUDGET
        ]
        skipped = [
            {"fc_hz": round(e.fc_hz, 1), "reason": e.refusal}
            for e in evaluations if e.refusal == EVAL_REFUSED_BUDGET
        ]
        comparison_complete = fc_comparison_complete(
            evaluations, len(candidates.candidates)
        )
        journal(GateRecord(event=EVENT_SWEEP, fields={
            "configured_hz": round(configured_fc_hz, 1),
            "planned": len(candidates.candidates),
            "evaluated": sum(1 for e in evaluations if e.refusal is None),
            "candidate_order": [round(fc, 1) for fc in candidates.candidates],
            "attempted": attempted,
            "skipped": skipped,
            "comparison_complete": comparison_complete,
            "elapsed_s": round(time.monotonic() - started, 2),
            "budget_s": round(budget_s, 2),
            "limits": {k: round(v, 1) for k, v in candidates.limits.items()},
            "rejected": [
                [round(fc, 1), reason] for fc, reason in candidates.rejected
            ],
        }))
    except _SWEEP_ERRORS as exc:
        # The whole advisory declined, loudly. Same caught set as the
        # per-candidate handler above; ``CrossoverV2FlowError`` is a
        # ``RuntimeError``, so a refused candidate-set derivation lands here
        # rather than escaping into the capture's accept path.
        #
        # **The handler's own disclosure is guarded in turn**, because it goes
        # through the same port that may be what just failed. Unguarded, a
        # journal raising here replaces a CONTAINED advisory failure with an
        # escaping one — the household loses the completed MEASURE to the
        # second fault rather than the first, which is the #2313 SF1
        # double-fault shape. There is nowhere left to report the loss TO: the
        # journal IS the reporting channel. So it is swallowed, deliberately
        # and only here, and the evaluations still return.
        try:
            journal(GateRecord(
                event=EVENT_SWEEP_REFUSED,
                level=logging.WARNING,
                fields={"reason": type(exc).__name__},
            ))
        except _SWEEP_ERRORS:
            pass
    return tuple(evaluations)


@dataclass(frozen=True)
class Adjudication:
    """The one recommendation a sweep's evidence became, and the corner to commit.

    Three facts rather than one: ``selection`` is what the review screen renders,
    ``selected_evaluation`` is the executable candidate Review applies after
    Sound-owned acceptance, and ``record`` is the disclosure the host still owes.
    ``None`` for the evaluation is ordinary — the selector may recommend the
    configured corner, or none at all — and it is deliberately not derivable
    from ``selection`` alone, because matching a recommended frequency back to
    its evaluation is the step that can come up empty.

    **Why the record is RETURNED here and handed to a port in
    :func:`sweep_candidates`.** The two are not inconsistent, they answer
    different facts. The sweep's lines are interleaved with the accountability
    lines each build emits, so batching them to the end would re-order the
    journal; they must be said at the point. Adjudication's single line is the
    last thing it does, so returning it re-orders nothing — and it buys back the
    pre-extraction failure ordering, where the selection is published BEFORE the
    line is said. A journal that raises then costs the household a log line
    rather than the recommendation it was about.
    """

    selection: FcSelection
    selected_evaluation: FcCandidateEvaluation | None
    record: GateRecord


def adjudicate(
    evaluations: Sequence[FcCandidateEvaluation],
    pose_curves: Sequence[Any],
    *,
    select: Callable[..., FcSelection],
    candidate_set_of: Callable[[], FcCandidateSet],
    configured_fc_hz: float,
) -> Adjudication:
    """Turn the retained per-candidate evidence into ONE recommendation.

    At the walk's close, where §4.4 puts every judgement that reads the
    whole walk. The caller releases the evaluations after: the selection is
    what the review screen renders, and the evidence behind it has done its job.

    Says nothing itself — see :class:`Adjudication` for why this one disclosure
    travels back as data while the sweep's are handed to a port.
    """
    candidates = candidate_set_of()
    selection = select(
        evaluations,
        pose_curves,
        configured_hz=configured_fc_hz,
        limits=candidates.limits,
        planned=len(candidates.candidates),
    )
    recommended = selection.recommended_hz
    selected: FcCandidateEvaluation | None = None
    if recommended is not None:
        selected = next(
            (e for e in evaluations
             if math.isclose(e.fc_hz, recommended, abs_tol=0.05)
             and e.candidate is not None), None)
    record = GateRecord(event=EVENT_SELECTION, fields={
        "verdict": selection.verdict,
        "configured_hz": round(configured_fc_hz, 1),
        "recommended_hz": (
            round(selection.recommended_hz, 1)
            if selection.recommended_hz is not None else None
        ),
        "margin_db": (
            round(selection.margin_db, 3)
            if selection.margin_db is not None else None
        ),
        "evaluated": selection.evaluated,
        "planned": selection.planned,
        "candidate_order": [round(fc, 1) for fc in selection.candidate_order],
        "attempted": [round(fc, 1) for fc in selection.attempted],
        "skipped": [
            {"fc_hz": round(fc, 1), "reason": reason}
            for fc, reason in selection.skipped
        ],
        "comparison_complete": selection.comparison_complete,
        "poses": len(pose_curves),
    })
    return Adjudication(
        selection=selection, selected_evaluation=selected, record=record,
    )
