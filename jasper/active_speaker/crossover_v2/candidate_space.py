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
* the ka beaming ceiling, through
  :func:`~jasper.active_speaker.branch_chain.beaming_onset_hz`;
* the legal Linkwitz-Riley orders, through
  :data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`;
* the delay lobe, through
  :func:`~jasper.audio_measurement.program_analysis.half_period_us`.

Two bounds this module does not resolve, because neither is a declaration
about a component — each is a property of the CHAIN being proposed into. The
caller declares both and this file never guesses either: the realizable delay
STEP (:attr:`CandidateBounds.delay_step_us`, #2710) and the STANDING high-pass
a branch already carries outside the crossover
(:attr:`CandidateBounds.standing_highpass_hz_by_role`, #2760).

**Three sources of bound, with a precedence, because they are not the same kind
of fact.** Adopted from the owner's crossover-design research review; the house
fresh-eyes rule stated for this module is **priors seed the search and
measurements decide**.

1. **Declared driver safety — HARD.** The protection floor above. It is a
   claim a person confirmed about a component, it binds regardless of what any
   measurement says, and it is the one bound the apply gate re-checks.
2. **Measured directivity — a HARD ceiling and a SOFT prior.** The literature's
   primary Fc selector is directivity matching: cross where the woofer's
   narrowing beamwidth meets the horn's coverage. Two numbers come out of
   per-driver per-angle data, and they are different kinds of thing.
   :attr:`CandidateBounds.directivity_ceiling_hz` — the woofer's -6 dB at
   30 degrees off axis — is a measured ceiling and REFUSES above it.
   :attr:`CandidateBounds.matched_beamwidth_hz` is where the two coverages
   meet: an excellent place to start looking and not a fence, so it never
   refuses.
3. **Geometry priors — SOFT, seed only, never binding.** The ka beaming
   ceiling is modelled from a declared radius, not measured, so it may inform
   or warn and may never refuse. #1675 rules it guidance, and on jts3 it has
   sat BELOW that speaker's own configured corner — a bound there would refuse
   the crossover the speaker is running. (Deliberately no numbers: the corner
   is per-speaker and moves. ``fc_selector.score_candidate``'s docstring
   quotes a "jts3: 1915.4 ceiling, 2000 configured" pair that the 2026-08-18
   bank contradicts at 1648.7 — pre-existing staleness in a module this plan
   supersedes, recorded rather than edited here.)

The measured layer is deliberately NOT produced by
:func:`bounds_from_declarations`, whose whole contract is that it reads
declarations only. It arrives through
:meth:`CandidateBounds.with_measured_directivity`, so a reader can always tell
which bounds were declared and which were measured.

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

Since #2760 the two doors read the same rule against a DIFFERENT number in one
case, and it is bounded by a caller contract rather than by code. The backstop
reads the crossover corner alone; this door reads the whole branch, so a
:attr:`CandidateBounds.standing_highpass_hz_by_role` entry on the role the
crossover itself protects would admit exactly the candidate the backstop then
refuses. Nothing structural stops a caller writing one — the field's contract
below forbids it instead, and the reason that is the right trade is recorded
there.

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

**Stricter than the backstop is allowed; looser never is.** The two rules above
are about the floor's VALUE, which must match exactly. An ADDITIONAL condition
that only ever refuses more than the apply gate would is safe, because the
backstop still catches everything this door lets through. That asymmetry is
what makes the slope question below a legitimate future refinement rather than
a contradiction.

"Looser never" is a rule this file obeys and no longer *proves*, and the
distinction matters to whoever edits next. Within a candidate it still holds
absolutely: :func:`_protecting_highpass_hz` compares the candidate's own
crossover corner whenever it proposes one, so no standing value can rescue a
corner a candidate actually asks for. What it cannot bound is a caller that
declares a standing high-pass for the crossover-protected role — see
:attr:`CandidateBounds.standing_highpass_hz_by_role`, which forbids it in
prose because the guard that would enforce it needs a protected-role name this
package publishes no owner for.

**A high-pass the candidate does not propose is still protection (#2760).** A
two-way crossover gives the LOWER driver a low-pass and never a high-pass, so a
woofer declaring a 40 Hz protective floor has no crossover corner to compare it
against. Reading that as "unprotected" refused every candidate in the space —
1681 of 1681, the incumbent included — the first time a confirmed driver-safety
profile was supplied, and the session could only proceed by withholding the
woofer's floor.

The emitted graph disagrees, because the crossover is not the only filter on
the branch. ``camilla_yaml._driver_baseline_filter_chain`` prepends the
bass-management high-pass AHEAD of the crossover; a sealed bass-extension
profile adds a subsonic high-pass; and the crossover-v2 measurement graph emits
each role's own confirmed ``required_protection_filters`` (through
:func:`~jasper.active_speaker.branch_chain.confirmed_protection_sections`).
None of those is a crossover section: no candidate proposes them and no
candidate can remove them.

Not one of them is unconditional, and they do not stack. The confirmed
protection sections belong to the MEASUREMENT graph while bass management
belongs to the applied baseline, and ``camilla_yaml._bass_management_active``
emits even that one only when the preset carries a local subwoofer; the sealed
subsonic filter sits far below any floor it would be asked to satisfy. So the
corner a caller owes is the APPLIED graph's, which in practice means the
bass-management high-pass — the only one of the three both routinely present
and high enough to answer a real floor.

This module may not assume that corner exists any more than it may assume it
does not. The caller declares what its chain actually carries on
:attr:`CandidateBounds.standing_highpass_hz_by_role`; absent means absent, and
a driver left genuinely open below its declared floor is refused by the same
slug as before.

**A standing filter never rescues a corner the candidate itself proposes.**
When a candidate high-passes a role, THAT corner is the number compared to the
floor and the standing value is not consulted. This is what keeps the front
door a superset of the backstop rather than a hole in it: the apply gate reads
the CROSSOVER corner alone, so crediting a standing filter against a too-low
proposed corner would admit a candidate the apply door then refuses — after the
Sound declaration is written, which is the ordering failure this whole pairing
exists to prevent.

**Slope is NOT part of the floor comparison, and this PR is what makes that
observable.** The shared predicate compares corner FREQUENCIES only
(``highpass_hz >= floor_hz``); it has no slope term, and neither does the apply
gate. A declaration may still carry
``required_protection_filters[highpass].minimum_slope_db_per_octave``, and this
module is the first consumer to make order a searchable axis — the corner sweep
it supersedes hard-coded LR4 — so a candidate can now propose a shallower slope
than a declaration requires and pass the frequency check.

Deferred rather than closed, and the reason is EVIDENCE rather than layering.
The declared floor is really a distortion/excursion bound, so a steeper
high-pass legitimately permits a lower crossing — it attenuates the sub-Fs
excursion region faster. A slope-aware ``floor(slope)`` is therefore a real
refinement this module MAY take, but only against a source that can validate
it: the banked Novak distortion-versus-frequency captures are the candidate.
Two rules bind whoever takes it. It may only ever make this door **stricter**
— admitting a candidate the apply gate would refuse is forbidden, and a
naive reading of "steeper permits lower" does exactly that. And the apply gate
stays slope-blind and conservative on purpose; it is the backstop, not the
place to encode a curve.

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
``_deg`` degrees, ``_mm`` millimetres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from jasper.audio_measurement.program_analysis import half_period_us

from ..branch_chain import CrossoverSection, beaming_onset_hz
from ..driver_protection import (
    declared_protection_highpass_floor_hz, protection_highpass_floor_satisfied,
)
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
    "REJECT_ABOVE_DIRECTIVITY_CEILING",
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
REJECT_ABOVE_DIRECTIVITY_CEILING = "above_measured_directivity_ceiling"
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
    REJECT_ABOVE_DIRECTIVITY_CEILING,
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
    corner never exposes a driver below its floor.

    Per-role rather than the corner sweep's two-way ``hf_hard_floor_hz``, and
    that IS a widening rather than a restatement: the sweep compares one floor,
    the HF role's band edge (``crossover_v2_flow``'s ``hf_hard_floor_hz``), and
    a per-role map admits floors the sweep never had a term for. #2760 is what
    that widening cost when the extra floors were compared to the crossover
    alone — see ``standing_highpass_hz_by_role`` below and the module docstring.

    ``standing_highpass_hz_by_role`` is what the branch ALREADY high-passes at,
    outside the crossover, on the chain being proposed into — declared by the
    caller for the same reason ``delay_step_us`` is, and read only for a role
    the candidate does not high-pass itself. A role with no entry has no
    standing high-pass; the module docstring enumerates the graph's possible
    sources for one and why each is conditional, which is why an absent entry
    must mean absent rather than "probably fine".

    **The graph that counts is the one the candidate will be APPLIED to**, not
    whichever graph is playing while the search runs: a shortlist measured on
    the crossover-v2 program graph is applied as a baseline. Which of the
    graph's standing filters that leaves you declaring, and why the other two
    are not it, is the module docstring's to say.

    Declare it for the role the chain high-passes OUTSIDE the split — in a
    two-way that is the woofer, whose branch the emitter high-passes ahead of
    the crossover and whose floor no candidate can honour. Do NOT declare one
    for the role the crossover itself protects: the apply gate compares that
    role's CROSSOVER corner and nothing else, so a standing entry there could
    only ever admit a candidate the apply door then refuses. That contract is
    the whole guard, deliberately: enforcing it in code means naming the
    protected role, which every sibling gate spells as a bare literal because
    the package publishes no owner for it (``crossover_declaration``'s
    ``_PROTECTED_ROLE`` comment says exactly that), and adding one more
    unowned spelling here would cost more than the contract does.

    ``beaming_ceiling_hz`` is GUIDANCE and never refuses. #1675 defines the ka
    prior as something to warn on rather than a fence, and on jts3 the ceiling
    has sat below that speaker's own configured corner — a bound that refused
    it would refuse the crossover the speaker is running. It is published here
    so a shortlist can disclose it.

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
    # The CHAIN's standing protection, defaulted empty so a caller that declares
    # nothing gets exactly the pre-#2760 wall rather than a silent credit.
    standing_highpass_hz_by_role: Mapping[str, float] = MappingProxyType({})
    # The MEASURED layer, defaulted absent because it does not exist until a
    # per-driver per-angle capture does. Supplied through
    # ``with_measured_directivity`` rather than by ``bounds_from_declarations``,
    # so declared and measured provenance never blur together.
    directivity_ceiling_hz: float | None = None
    matched_beamwidth_hz: float | None = None

    @property
    def proposable(self) -> bool:
        """Whether any Fc may be proposed at all.

        ``False`` is an honest verdict rather than a failure: the incumbent is
        still evaluated, and "keep what is configured" is an answer.
        """
        return self.fc_band_hz is not None

    def with_measured_directivity(
        self,
        *,
        directivity_ceiling_hz: float | None = None,
        matched_beamwidth_hz: float | None = None,
    ) -> "CandidateBounds":
        """These bounds plus what a per-driver per-angle capture measured.

        A separate entry point from :func:`bounds_from_declarations` on
        purpose. That function's contract is that it reads DECLARATIONS, and a
        measured ceiling is a different kind of fact with a different
        precedence — mixing them into one constructor would leave a reader
        unable to tell which bounds a person confirmed and which a microphone
        did. Both arguments are optional because a session may have measured
        one and not the other.

        ``directivity_ceiling_hz`` is the woofer's -6 dB at 30 degrees off
        axis: crossing above it hands the horn a band the cone has already
        stopped covering off axis, which no on-axis EQ repairs. It REFUSES.

        ``matched_beamwidth_hz`` is where the two coverages meet. It is the
        literature's primary Fc selector and an excellent seed, but it is a
        target rather than a limit — a candidate away from it may still measure
        better, and this file does not get to decide that in advance. It never
        refuses; the search reads it.

        Neither number is computed here. Deriving them is per-angle
        measurement work that belongs to whoever holds the captures; this
        module's job is to carry them with the right force.
        """
        return replace(
            self,
            directivity_ceiling_hz=(
                self.directivity_ceiling_hz if directivity_ceiling_hz is None
                else float(directivity_ceiling_hz)
            ),
            matched_beamwidth_hz=(
                self.matched_beamwidth_hz if matched_beamwidth_hz is None
                else float(matched_beamwidth_hz)
            ),
        )


def bounds_from_declarations(
    *,
    drivers_by_role: Mapping[str, Any],
    search_band_hz_by_role: Mapping[str, tuple[float, float] | None],
    excitation_ceiling_hz_by_role: Mapping[str, float],
    lower_driver_diameter_mm: float | None = None,
    incumbent_fc_hz: float,
    geometry_seed_us: float,
    delay_step_us: float,
    standing_highpass_hz_by_role: Mapping[str, float] | None = None,
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

    ``drivers_by_role`` accepts either the preset's parsed driver specs (read
    off ``protection_highpass_floor_hz``, the field the apply gate reads) or a
    raw driver payload mapping (routed through
    ``driver_protection.declared_protection_highpass_floor_hz``, the function
    that PRODUCES that field). Both end at one parse of one fact, so the front
    door and the backstop cannot disagree.

    **Both, and not just the spec, because ``getattr`` on a dict is silent.** An
    earlier version read the attribute only. Handed a raw payload — which reads
    perfectly naturally at a call site — every lookup returned ``None``, every
    role lost its floor, and the safety wall disappeared with no error anywhere.
    A fail-open that a caller cannot see is worse than a refusal it can.

    **A role with no declared floor gets no entry, and that is unbounded below
    rather than a floor of zero or a style default.** ``None`` means the
    derived protection is unclamped on the emitted graph too, and
    :func:`protection_highpass_floor_satisfied` reads an absent floor as
    satisfied. See the module docstring for why matching the apply layer
    exactly matters more here than being conservative.

    ``standing_highpass_hz_by_role`` is passed through unresolved, exactly like
    ``delay_step_us``: it is a fact about the CHAIN, not a declaration about a
    component, and this function's contract is declarations. Resolving it would
    mean reading the preset's bass-management state from a module whose whole
    promise is that it reads none — and the caller is the one holding the graph
    it is proposing into.

    The delay window is ``geometry_seed_us`` plus or minus one half-period at
    the incumbent corner — the lobe
    :func:`~jasper.audio_measurement.program_analysis.half_period_us` owns, and
    the same bound ``alignment_prescription`` refuses a prescription against.
    Two delays further apart than this sit on adjacent comb lobes, where a
    score can land on the wrong one and look just as good locally.
    """
    floors: dict[str, float] = {}
    for role in sorted(drivers_by_role):
        floors_hz = _declared_floor_hz(drivers_by_role[role])
        if floors_hz is not None:
            floors[role] = floors_hz

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
        standing_highpass_hz_by_role={
            str(role): float(hz)
            for role, hz in (standing_highpass_hz_by_role or {}).items()
        },
    )


def _declared_floor_hz(driver: Any) -> float | None:
    """One driver's declared protection floor, however the caller holds it.

    A parsed :class:`~jasper.active_speaker.profile.DriverSpec` carries the
    value on ``protection_highpass_floor_hz``; a raw payload mapping has not
    been parsed yet and goes through
    :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`,
    which is the function that fills that field in the first place. Two shapes,
    one parse, one number.

    The ``Mapping`` branch is checked FIRST and is not defensive padding: a
    ``dict`` answers ``getattr`` with the default, so reading the attribute
    first would turn every raw payload into "no floor declared" — silently,
    with no error, taking the safety wall down with it.
    """
    if isinstance(driver, Mapping):
        floor_hz = declared_protection_highpass_floor_hz(driver)
    else:
        floor_hz = getattr(driver, "protection_highpass_floor_hz", None)
    return None if floor_hz is None else float(floor_hz)


def strictest_highpass_hz(candidate: XoverCandidate, role: str) -> float | None:
    """The strictest (highest) corner that high-passes ``role``, or ``None``.

    The candidate-side twin of
    :func:`~jasper.active_speaker.test_signal_plan.strictest_crossover_highpass_hz`,
    which reads the same quantity off a compiled preset (``max`` of the
    regions whose ``upper_driver`` is this role). Same rule, same ``max``, same
    ``None`` — the two have to answer one question about one number, and the
    question is *the crossover's* corner rather than the branch's protection.

    ``max`` and not ``min``: cascading high-passes compound, so a role
    high-passed at both 500 Hz and 2000 Hz is protected to 2000. Taking the
    lowest corner would refuse a fully-protected branch for a section that is
    not the one doing the protecting.

    ``None`` means this CANDIDATE gives the role no high-pass — not that the
    role has none, which is a question about the whole branch and is
    :func:`_protecting_highpass_hz`'s to answer (#2760: a two-way's woofer is
    never high-passed by the split, yet the chain may high-pass it ahead of
    one). Do not compare this value to a declared floor directly; reading its
    ``None`` as "the driver runs open" is the refusal that zeroed a search.
    """
    corners = [
        float(section.fc_hz)
        for section in candidate.sections_by_role.get(role, ())
        if section.highpass
    ]
    return max(corners) if corners else None


def _protecting_highpass_hz(
    candidate: XoverCandidate, bounds: CandidateBounds, role: str,
) -> float | None:
    """The high-pass corner ``role``'s branch is protected at, or ``None``.

    The candidate's own crossover high-pass when it proposes one, otherwise the
    standing high-pass the caller declared for that role on the chain. ``None``
    means nothing high-passes the branch at all — the hazard the floor exists
    for, and the reading :func:`protection_highpass_floor_satisfied` refuses.

    **The candidate's corner WINS rather than combining with the standing one,
    and that asymmetry is the safety property.** A ``max`` of the two would be
    the physically complete answer — cascaded high-passes compound, so a branch
    carrying both is protected at the higher — but it would also let a standing
    filter rescue a crossover corner the candidate actively proposes below a
    declared floor. The apply gate reads the CROSSOVER corner alone, so that
    candidate would be admitted here and refused there, one durable Sound write
    too late. Refusing it here is the module docstring's "stricter than the
    backstop is allowed; looser never is", applied to the one case where the
    two answers differ.

    So the standing value is consulted only when the crossover says nothing —
    which is exactly the two-way lower driver #2760 is about, and never the
    upper driver whose high-pass IS the split.
    """
    crossover_hz = strictest_highpass_hz(candidate, role)
    if crossover_hz is not None:
        return crossover_hz
    standing_hz = bounds.standing_highpass_hz_by_role.get(role)
    return None if standing_hz is None else float(standing_hz)


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

    **Why each wall is its own pass over the roles.** Two independent reasons,
    both found by a test rather than by reading.

    An undeclared role poisons the whole bounds object rather than only its own
    entry — the intersection is ``None`` for everyone — so that check has to sit
    between the per-role walls and the band. In one loop the FIRST role walked
    reaches the absent band before the LAST role gets to say it is the
    undeclared one, and the refusal names the symptom.

    The floor is worse, because a single loop over the CANDIDATE's roles cannot
    see the hazard at all. A role that declares a floor and carries no readable
    high-pass corner — an empty sections tuple, only a low-pass, or no entry in
    ``sections_by_role`` — has nothing for a per-section check to iterate, so it
    fell through and was ADMITTED, while the emit gate refuses exactly that
    case. The floor pass therefore walks
    :attr:`CandidateBounds.declared_floor_hz_by_role`, not the candidate's own
    roles: the declaration decides who gets checked, and a candidate cannot opt
    out of a wall by staying silent about the driver it protects.

    What silence does NOT mean is "no filter" — only "no filter this candidate
    proposes". :func:`_protecting_highpass_hz` is the one number the floor is
    compared against, and #2760 is what reading a bare crossover corner instead
    cost: a two-way's woofer is never high-passed by the split, so its declared
    floor refused the entire space including the incumbent.

    Roles are walked in sorted order so a candidate with two violations always
    reports the same one. A refusal that varies with dictionary order is a
    refusal an operator cannot reproduce.

    **Never a clamp, and never silent.** The caller gets a slug from
    :data:`CANDIDATE_REFUSAL_REASONS` or a candidate it may score. There is no
    third outcome and no adjusted candidate.
    """
    # Pass 1 — the safety wall, driven by the DECLARATIONS rather than by the
    # candidate: one high-pass per role through the one shared predicate, so
    # "floor declared, nothing high-passing this role" refuses and "no floor
    # declared" is honoured, on this door and on the emit gate alike.
    #
    # The emit gate compares the CROSSOVER corner and only the tweeter's
    # (``camilla_yaml._assert_tweeter_crossover_honours_declared_floor``); this
    # door is per-role, so it also has to know what else high-passes a role.
    # ``_protecting_highpass_hz`` is that one number.
    for role in sorted(bounds.declared_floor_hz_by_role):
        if not protection_highpass_floor_satisfied(
            highpass_hz=_protecting_highpass_hz(candidate, bounds, role),
            floor_hz=bounds.declared_floor_hz_by_role.get(role),
        ):
            return FC_REJECT_BELOW_DECLARED_FLOOR

    for role in candidate.roles:
        for section in candidate.sections_by_role.get(role, ()):
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
            if bounds.directivity_ceiling_hz is not None and fc_hz > float(
                bounds.directivity_ceiling_hz
            ):
                # MEASURED, so it refuses — unlike the ka prior below it, which
                # is modelled from a declared radius and only ever informs.
                return REJECT_ABOVE_DIRECTIVITY_CEILING
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
