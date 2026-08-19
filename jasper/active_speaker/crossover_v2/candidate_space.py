# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a crossover candidate IS, and which candidates are legal.

Pure data and legality. No physics, no scoring, no I/O — :mod:`.forward_model`
predicts what a candidate sounds like, and this module only says whether it may
be proposed at all.

**One expression of each wall, read twice.** Every bound here is resolved from a
DECLARATION by the module that owns that declaration — this file imports those
owners and never restates their arithmetic:

* the tweeter's declared protective high-pass floor, read from
  :attr:`~jasper.active_speaker.profile.DriverSpec.protection_highpass_floor_hz`
  — the value
  :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`
  parses ONCE onto the preset — and compared through the shared predicate
  :func:`~jasper.active_speaker.driver_protection.protection_highpass_floor_satisfied`;
* the intersected declared search band, through
  :func:`~jasper.active_speaker.crossover_v2.fc_sweep.resolve_fc_search_band`;
* the beaming ceiling, through
  :func:`~jasper.active_speaker.branch_chain.beaming_onset_hz`;
* the legal Linkwitz-Riley orders, through
  :data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`;
* the delay lobe, through
  :func:`~jasper.audio_measurement.program_analysis.half_period_us`.

The realizable delay STEP is the one bound this module does not resolve: it is
a property of the chain being proposed into (#2710), so the caller declares it
on :attr:`CandidateBounds.delay_step_us` and this file never guesses one.

**Never a clamp.** :func:`refusal_for` returns a named slug or ``None``. A
proposal outside a bound is refused by name, never pulled to the boundary and
run: a silently different candidate is worse than no candidate, because the
round then grades something nobody proposed.

**The front door, with the apply gate as the backstop.** This module and the
apply transaction check the SAME rule against the SAME number, deliberately,
because they defend against different failures — and they must therefore agree
exactly. The reason is a measured sequencing defect rather than a principle:
the alternative-apply door writes the Sound declaration BEFORE the graph apply,
so a below-floor candidate that reaches the door refuses AFTER the declaration
is saved and surfaces to a household as an ambiguous "could not confirm whether
the DSP apply finished". A below-floor Fc must therefore never become a
proposable candidate at all.

Two consequences for how the floor is read here, both non-obvious:

* **The value comes from the same parse, not from a second resolver.** An
  earlier draft of this module used
  :func:`~jasper.active_speaker.driver_protection.resolve_driver_low_limit`,
  which falls back to a per-style code default when a driver declares nothing.
  That would have made the front door STRICTER than the backstop — refusing
  proposals the apply path accepts — which is the 2026-08-14 never-nanny ruling
  in reverse and re-creates the divergence this pairing exists to remove.
* **An undeclared floor is unbounded below, and that is the apply layer's
  semantics rather than a permissive shortcut.** ``None`` means no floor was
  declared; the derived protection is then unclamped, exactly as it is on the
  emitted graph. Inventing a floor here that the graph does not enforce would
  again make the two disagree.

**Slope is NOT part of the floor comparison, and this PR is what makes that
observable.** The shared predicate compares corner FREQUENCIES only
(``highpass_hz >= floor_hz``); it has no slope term, and neither does the apply
gate. A declaration may still carry
``required_protection_filters[highpass].minimum_slope_db_per_octave``, and this
module is the first consumer to make order a searchable axis — the corner sweep
it supersedes hard-coded LR4 — so a candidate can now propose a shallower slope
than a declaration requires and pass the frequency check. That gap is
**disclosed rather than closed here on purpose**: enforcing slope in the front
door while the backstop ignores it would rebuild the front-door-stricter
asymmetry described above. Closing it means changing the shared predicate, and
that is an owner's call, not a proposer's.

**Why the shared refusal slugs are imported from** :mod:`.fc_sweep`. The three
Fc walls below are already spelled there, and a journal slug is a grep contract
— two spellings of ``below_declared_floor`` would be two vocabularies for one
wall. This module adds slugs only for the axes ``fc_sweep`` never had (order,
polarity, delay, gain). The plan that supersedes ``fc_sweep`` relocates those
three constants here and inverts this import; until then the owner at HEAD is
the owner this file reads.

**Filter type is not a search axis, and this file does not pretend otherwise.**
:data:`~jasper.active_speaker.profile.SUPPORTED_CROSSOVER_TYPES` is a
one-element set and
:class:`~jasper.active_speaker.branch_chain.CrossoverSection` has no type field
at all, so a non-Linkwitz-Riley candidate is not REPRESENTABLE rather than
merely illegal. There is no slug for it because there is no way to write one
down. Slope is carried as ``order`` (LR order N is 6*N dB/octave), which is the
vocabulary the emitted graph and the design draft both already speak.

**Units are in every name**, because the sign convention is the convention of
record: ``_hz`` frequencies, ``_us`` microseconds, ``_db`` decibels,
``_deg`` degrees.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.audio_measurement.program_analysis import half_period_us

from ..branch_chain import CrossoverSection, beaming_onset_hz
from ..driver_protection import protection_highpass_floor_satisfied
from ..profile import SUPPORTED_LR_ORDERS
from .fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_OUTSIDE_SEARCH_BAND,
    resolve_fc_search_band,
)

__all__ = [
    "CANDIDATE_REFUSAL_REASONS",
    "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
    "FC_REJECT_BELOW_DECLARED_FLOOR",
    "FC_REJECT_OUTSIDE_SEARCH_BAND",
    "REJECT_DELAY_NOT_REALIZABLE",
    "REJECT_DELAY_OUTSIDE_WINDOW",
    "REJECT_GAIN_OUTSIDE_RANGE",
    "REJECT_INVALID_POLARITY",
    "REJECT_UNDECLARED_ROLE",
    "REJECT_UNSUPPORTED_ORDER",
    "CandidateBounds",
    "XoverCandidate",
    "bounds_from_declarations",
    "refusal_for",
]

# The walls the Fc axis shares with the corner sweep are imported above rather
# than re-spelled. These four are this module's own, one per axis the sweep
# never had.
#
# Named codes and not bare booleans for the reason ``fc_sweep`` gives about its
# own: each of these is an operator- or household-actionable declaration, so a
# refusal has to say WHICH declaration refused it and therefore where to go.
REJECT_UNSUPPORTED_ORDER = "unsupported_lr_order"
REJECT_INVALID_POLARITY = "polarity_not_plus_or_minus_one"
REJECT_DELAY_OUTSIDE_WINDOW = "delay_outside_declared_window"
REJECT_DELAY_NOT_REALIZABLE = "delay_not_realizable_on_the_chain"
REJECT_GAIN_OUTSIDE_RANGE = "gain_outside_declared_range"
REJECT_UNDECLARED_ROLE = "role_has_no_declared_bounds"

#: Every slug :func:`refusal_for` can return. Closed on purpose: a consumer
#: rendering a refusal to a household needs the set to be enumerable, and a
#: typo'd slug should fail a test rather than reach a page as mystery text.
CANDIDATE_REFUSAL_REASONS = frozenset({
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_OUTSIDE_SEARCH_BAND,
    REJECT_DELAY_NOT_REALIZABLE,
    REJECT_DELAY_OUTSIDE_WINDOW,
    REJECT_GAIN_OUTSIDE_RANGE,
    REJECT_INVALID_POLARITY,
    REJECT_UNDECLARED_ROLE,
    REJECT_UNSUPPORTED_ORDER,
})

# How far off the declared delay step a proposal may land, microseconds.
#
# Half a formatter tick. The emitted YAML carries delays through
# ``jasper.camilla_emit.fmt`` at 4 decimal places of milliseconds — a 0.1 us
# lattice — so no value can be more than 0.05 us from something the graph can
# spell, and a proposal inside that distance of a declared step is on it.
#
# **The formatter lattice is deliberately NOT a wall of its own**, and the
# arithmetic says why rather than taste: quantizing to a 0.1 us grid moves a
# value by at most 0.05 us, so a check against it either never fires or refuses
# almost everything, depending on which side of 0.05 the tolerance sits. At
# 20 kHz, 0.05 us is 0.36 degrees of phase. A gate that cannot fire is worse
# than no gate, because it reads as coverage — so the formatter is named here
# as considered-and-rejected and the only delay lattice that binds is the one
# the caller DECLARES.
#
# That declared step is #2710's +/-20.833 us integer-sample alignment on a
# 48 kHz chain, which is four hundred times coarser and is a real bound.
_DELAY_STEP_TOLERANCE_US = 0.05


@dataclass(frozen=True)
class XoverCandidate:
    """One crossover proposal, in the emitted graph's own vocabulary.

    Five axes, all per role so a three-way is expressible even though the
    bounds and enumeration around it are specified for a two-way:

    ``sections_by_role``
        The Linkwitz-Riley sections each branch runs through — corner,
        order and direction, exactly
        :func:`~jasper.active_speaker.branch_chain.sections_by_role`'s shape.
        Filter type is Linkwitz-Riley by construction (see the module
        docstring) and slope is ``6 * order`` dB/octave.
    ``polarity_by_role``
        ``+1`` or ``-1``, the branch's commanded polarity.
    ``delay_us_by_role``
        **RESIDUAL** delay in microseconds, signed, in the analysis frame —
        never an applied delay. See :func:`residual_delay_us` and the landmine
        it exists to defuse.
    ``gain_db_by_role``
        The emitted per-driver trim in dB, at or below unity.

    Frozen and hashable-by-content is deliberate: a shortlist that carries a
    candidate has to be able to say two entries are the same proposal, and a
    mutable mapping would make "the incumbent is always in the evaluated set"
    a claim about object identity rather than about the crossover.
    """

    sections_by_role: Mapping[str, tuple[CrossoverSection, ...]]
    polarity_by_role: Mapping[str, int]
    delay_us_by_role: Mapping[str, float]
    gain_db_by_role: Mapping[str, float]

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role this candidate says anything about, sorted.

        The union rather than the intersection: a role carrying a delay but no
        sections is a candidate that asks for something, and
        :func:`refusal_for` has to see it in order to refuse it.
        """
        return tuple(sorted(
            set(self.sections_by_role)
            | set(self.polarity_by_role)
            | set(self.delay_us_by_role)
            | set(self.gain_db_by_role)
        ))

    @property
    def corner_hz(self) -> tuple[float, ...]:
        """Every distinct crossover corner in this candidate, sorted, Hz.

        A two-way has one. Distinctness is by exact value because a corner is
        carried as the declared float it was proposed as; two roles sharing a
        crossover carry the SAME number, not two rounded ones.
        """
        return tuple(sorted({
            float(section.fc_hz)
            for sections in self.sections_by_role.values()
            for section in sections
        }))


def residual_delay_us(anchor_delay_us: float | None, applied_delay_us: float) -> float:
    """This module's re-export of the ONE residual-delay derivation.

    :attr:`XoverCandidate.delay_us_by_role` is a residual, and building it from
    an applied delay by hand is the ``fix-2`` failure mode:
    :func:`~jasper.audio_measurement.program_analysis._aligned_branch_tf`
    references each branch to its OWN direct peak, so the physical inter-driver
    gap is already out of the measured pair, and phasing that pair by the full
    applied delay counts the gap twice — which
    :func:`~jasper.audio_measurement.program_analysis.summed_model_residual_delay_us`
    records as injecting "a deep comb into the predicted sum on good
    measurements".

    A one-line re-export and not a second derivation: the arithmetic has one
    owner and this is a signpost to it, placed where a caller building a
    candidate will actually be looking.
    """
    from jasper.audio_measurement.program_analysis import (
        summed_model_residual_delay_us,
    )

    return summed_model_residual_delay_us(anchor_delay_us, applied_delay_us)


@dataclass(frozen=True)
class CandidateBounds:
    """What the search may consider, and which declaration set each edge.

    ``fc_band_hz`` is ``None`` when NO proposal is legal — an undeclared role or
    an empty intersection — and the edge owners stay named anyway, because
    "your woofer says at-or-below 1500 and your tweeter says at-or-above 2000"
    is the actionable sentence and a bare ``None`` throws it away.

    ``declared_floor_hz_by_role`` is the H1 wall: the minimum recommended
    crossover frequency each role's driver declares. It binds a role's
    HIGH-PASS corners only, which is the physically exact statement — a low-pass
    corner never exposes a driver below its floor — and it generalizes the
    corner sweep's two-way ``hf_hard_floor_hz`` without changing it, because in
    a two-way the single corner IS the HF role's high-pass corner.

    ``beaming_ceiling_hz`` is GUIDANCE and never refuses. #1675 defines the ka
    prior as something to warn on rather than a fence, and on live jts3 the
    ceiling (1915.4 Hz) sits below the configured corner (2000 Hz) — a bound
    that refused it would refuse the speaker's own running crossover. It is
    published here so a shortlist can disclose it.

    ``delay_step_us`` is what the chain can REALIZE, declared by the caller
    from the graph it is proposing into (#2710: per-role integer-sample
    alignment quantizes at +/-20.833 us on a 48 kHz chain). A search that
    proposed a finer delay than this would be proposing a number the emitted
    graph rounds away.
    """

    fc_band_hz: tuple[float, float] | None
    fc_lo_role: str | None
    fc_hi_role: str | None
    declared_floor_hz_by_role: Mapping[str, float]
    beaming_ceiling_hz: float | None
    legal_orders: frozenset[int]
    delay_window_us: tuple[float, float]
    delay_step_us: float
    gain_db_range: tuple[float, float]
    undeclared_roles: tuple[str, ...]

    @property
    def proposable(self) -> bool:
        """Whether any Fc may be proposed at all.

        ``False`` is an honest verdict rather than a failure: the incumbent is
        still evaluated, and "keep what is configured" is an answer.
        """
        return self.fc_band_hz is not None


def bounds_from_declarations(
    *,
    drivers_by_role: Mapping[str, Any],
    search_band_hz_by_role: Mapping[str, tuple[float, float] | None],
    excitation_ceiling_hz_by_role: Mapping[str, float],
    lower_driver_diameter_mm: float | None = None,
    incumbent_fc_hz: float,
    geometry_seed_us: float,
    delay_step_us: float,
    gain_db_range: tuple[float, float] = (-30.0, 0.0),
) -> CandidateBounds:
    """Resolve every bound from DECLARATIONS, calling each fact's owner.

    Nothing here is measured and nothing is guessed. Each argument is something
    a person confirmed at component entry or the chain declares about itself,
    and each bound below names the function that owns it.

    ``excitation_ceiling_hz_by_role`` is the upper wall — the lower driver's
    declared hard ceiling, which
    :func:`~jasper.active_speaker.excitation_safety_plan.resolve_driver_excitation_ceilings`
    confirms. It is passed in already resolved rather than re-derived here,
    because the confirmation is a property of the safety plan and this module
    has no business re-deciding it.

    ``drivers_by_role`` are the preset's parsed driver specs, duck-typed on
    ``protection_highpass_floor_hz`` alone. **Parsed specs and not raw payloads,
    deliberately:** that field is where
    ``driver_protection.declared_protection_highpass_floor_hz`` puts its single
    parse, and the apply gate reads the same field. Re-parsing a payload here
    would be a second read of one fact, which is how the front door and the
    backstop start disagreeing.

    **A role with no declared floor gets no entry, and that is unbounded below
    rather than a floor of zero or a style default.** ``None`` means the
    derived protection is unclamped on the emitted graph too, and
    :func:`protection_highpass_floor_satisfied` reads an absent floor as
    satisfied. See the module docstring for why matching the apply layer
    exactly matters more here than being conservative.

    The delay window is ``geometry_seed_us`` plus or minus one half-period at
    the incumbent corner — the lobe
    :func:`~jasper.audio_measurement.program_analysis.half_period_us` owns, and
    the same bound ``alignment_prescription`` refuses a prescription against.
    Two delays further apart than this sit on adjacent comb lobes, where a
    score can land on the wrong one and look just as good locally.
    """
    floors: dict[str, float] = {}
    for role in sorted(drivers_by_role):
        floor_hz = getattr(
            drivers_by_role[role], "protection_highpass_floor_hz", None,
        )
        if floor_hz is not None:
            floors[role] = float(floor_hz)

    search = resolve_fc_search_band(search_band_hz_by_role)
    band_hz = search.band_hz
    if band_hz is not None and excitation_ceiling_hz_by_role:
        # The lower driver's declared ceiling narrows the search band the same
        # way the corner sweep applies it: the STRICTEST declared ceiling binds,
        # because a corner above any participating role's ceiling asks that
        # driver to work above what its declaration permits.
        ceiling_hz = min(float(v) for v in excitation_ceiling_hz_by_role.values())
        band_hz = (band_hz[0], min(band_hz[1], ceiling_hz))
        if band_hz[0] >= band_hz[1]:
            band_hz = None

    lobe_us = half_period_us(float(incumbent_fc_hz))
    return CandidateBounds(
        fc_band_hz=band_hz,
        fc_lo_role=search.lo_role,
        fc_hi_role=search.hi_role,
        declared_floor_hz_by_role=dict(floors),
        beaming_ceiling_hz=(
            beaming_onset_hz(float(lower_driver_diameter_mm))
            if lower_driver_diameter_mm is not None else None
        ),
        legal_orders=frozenset(SUPPORTED_LR_ORDERS),
        delay_window_us=(
            float(geometry_seed_us) - lobe_us, float(geometry_seed_us) + lobe_us,
        ),
        delay_step_us=float(delay_step_us),
        gain_db_range=(float(gain_db_range[0]), float(gain_db_range[1])),
        undeclared_roles=search.undeclared_roles,
    )


def refusal_for(
    candidate: XoverCandidate, bounds: CandidateBounds,
) -> str | None:
    """The FIRST wall ``candidate`` violates, hardest first, or ``None``.

    Precedence, and it is chosen rather than incidental:

    1. **the declared protection floor**, because it is the only wall here that
       protects a driver, and a candidate violating it plus something else must
       send the operator to the component declaration first;
    2. **the LR order**, because an unemittable shape makes every later
       question moot;
    3. **an undeclared role**, before the band — an undeclared role is what
       PRODUCES an absent band, so reporting the band would name the symptom
       and hide the cause;
    4. the search band, then polarity, delay and gain.

    This keeps ``fc_sweep._fc_rejection``'s floor-first discipline rather than
    inventing a second precedence.

    **Why 1-2 are a separate pass over the roles.** An undeclared role poisons
    the whole bounds object rather than only its own entry — the intersection
    is ``None`` for everyone — so the check has to sit between the per-role
    walls and the band, not inside a single loop. In one loop the FIRST role
    walked reaches the absent band before the LAST role gets to say it is the
    undeclared one, and the refusal names the symptom. That is not
    hypothetical: it is what this function did until a test asked for the
    two-role case in the order that exposes it.

    Roles are walked in sorted order so a candidate with two violations always
    reports the same one. A refusal that varies with dictionary order is a
    refusal an operator cannot reproduce.

    **Never a clamp, and never silent.** The caller gets a slug from
    :data:`CANDIDATE_REFUSAL_REASONS` or a candidate it may score. There is no
    third outcome and no adjusted candidate.
    """
    for role in candidate.roles:
        for section in candidate.sections_by_role.get(role, ()):
            # The floor binds HIGH-PASS corners only. A low-pass corner never
            # asks a driver to work below its declared minimum, so refusing one
            # against this floor would be a bound with no physical claim under
            # it. An absent floor is satisfied — see the module docstring on
            # why absence is never read as zero.
            if section.highpass and not protection_highpass_floor_satisfied(
                highpass_hz=float(section.fc_hz),
                floor_hz=bounds.declared_floor_hz_by_role.get(role),
            ):
                return FC_REJECT_BELOW_DECLARED_FLOOR
            if int(section.order) not in bounds.legal_orders:
                return REJECT_UNSUPPORTED_ORDER

    if any(role in bounds.undeclared_roles for role in candidate.roles):
        return REJECT_UNDECLARED_ROLE

    for role in candidate.roles:
        for section in candidate.sections_by_role.get(role, ()):
            fc_hz = float(section.fc_hz)
            if bounds.fc_band_hz is None:
                # No proposal is legal at all, and every role IS declared — so
                # the cause is an empty intersection between declarations
                # rather than a missing one. ``fc_lo_role`` / ``fc_hi_role``
                # name the two that disagree.
                return FC_REJECT_OUTSIDE_SEARCH_BAND
            if fc_hz > float(bounds.fc_band_hz[1]):
                return FC_REJECT_ABOVE_LOWER_DRIVER_BAND
            if fc_hz < float(bounds.fc_band_hz[0]):
                return FC_REJECT_OUTSIDE_SEARCH_BAND

        polarity = candidate.polarity_by_role.get(role, 1)
        if polarity not in (1, -1):
            return REJECT_INVALID_POLARITY

        delay_us = float(candidate.delay_us_by_role.get(role, 0.0))
        lo_us, hi_us = bounds.delay_window_us
        if not math.isfinite(delay_us) or not lo_us <= delay_us <= hi_us:
            return REJECT_DELAY_OUTSIDE_WINDOW
        if _delay_refused_by_step(delay_us, bounds.delay_step_us):
            return REJECT_DELAY_NOT_REALIZABLE

        gain_db = float(candidate.gain_db_by_role.get(role, 0.0))
        lo_db, hi_db = bounds.gain_db_range
        if not math.isfinite(gain_db) or not lo_db <= gain_db <= hi_db:
            return REJECT_GAIN_OUTSIDE_RANGE

    return None


def _delay_refused_by_step(delay_us: float, step_us: float) -> bool:
    """Whether ``delay_us`` is finer than the chain the caller DECLARED.

    A non-positive ``step_us`` means the caller declared no step, and declaring
    no step cannot be grounds for refusal — this module will not invent a
    timing bound the caller did not state. See
    :data:`_DELAY_STEP_TOLERANCE_US` for why the emitter's own formatter
    lattice is not a second wall here.
    """
    if step_us <= 0.0:
        return False
    offset_us = abs(delay_us) % step_us
    return min(offset_us, step_us - offset_us) > _DELAY_STEP_TOLERANCE_US
