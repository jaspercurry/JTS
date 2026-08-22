# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Layer-1a driver-linearization fit engine (#1668 PR-C).

Consumes ONE driver's :class:`~jasper.audio_measurement.program_analysis.
DriverResponse` (the primary, gated, calibrated measurement) plus its
:class:`~jasper.active_speaker.linearization_envelope.EnvelopeCurve` (from
:func:`jasper.active_speaker.linearization_envelope.compose_envelope`) and
produces a cut-preferred PEQ/shelf fit that flattens the driver toward a
per-session target level, honoring the envelope's per-bin correction-depth
ceiling everywhere. Cut-PREFERRED, not cut-only: see "Boost is allowed" below
for the vocabulary that admits a lift and what bounds it. Pure computation: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:func:`jasper.correction.peq.design_peq` (the existing greedy cuts-only PEQ
designer, extended here — backward-compatibly — to accept a per-bin cut
ceiling). No I/O, no CamillaDSP emission — this module answers "what filters
would flatten this driver," nothing more. Wiring the result into the v2
session's candidate and emitting it at APPLY are separate concerns, owned
elsewhere.

See docs/active-speaker-tuning-layers-design.md "Layer 1a concretely" for
the adopted design this module implements (fit domain, adaptive band trim,
target level, cut-preferred/normalize-downward policy, per-bin caps).

**The allowed vocabulary is an INPUT, not a hardcode** (PR-L5). The fit core
takes a measured response, an envelope, and a :class:`FitVocabulary` — "what
moves am I allowed to make" — and returns filters. Nothing else about the
speaker's topology reaches it: a 3-way active speaker fits three drivers
through this same function, and a passive box is the 1-way case fitted on its
summed chain. Do not add a way-count, a role branch, or a crossover
assumption here; they belong to whoever composes the vocabulary.

**Boost is allowed, uncapped, and evidence-gated** (PR-L5, owner ruling
2026-07-27). A cut-only fit cannot represent "this driver is 9 dB dark", so
for eight months the only way to raise a band was to cut everything else and
give the level back through the trim — a realization that runs out at
:data:`HF_SINGLE_SHELF_SPEND_CAP_DB`, which is where the 2026-07-27 profile
stopped ~3 dB short of its own measured deficit. :data:`FitVocabulary.
allow_boost` lifts that: the lift stage may emit ``gain > 0``, with no policy
cap on the total. What replaced the cap is not nothing — it is the closed-loop
delta probe (:mod:`jasper.active_speaker.delta_probe`), which measures what
the speaker actually did and rolls the correction back automatically when it
does not match. This module's docstring used to say boost was deferred "until
the closed-loop verify machinery exists"; it exists, and this is the
capability it was holding.

What did NOT change: the headroom cost of a boost is **disclosed**
(:attr:`LinearizationFit.headroom_cost_db`) and absorbed by the emitter's
existing ``active_baseline_headroom`` gain, exactly as room-correction boost
already is — so the CamillaDSP 0 dB ceiling, the per-driver limiters, and
tweeter protection remain untouched hard rails. What that cost IS changed on
2026-07-28 (#1808): the realized peak of the emitted branch chain rather than
the sum of positive filter gains, because the sum charged a JTS3 profile
22.458 dB for a branch whose true peak was +4.00 dB and left the speaker 8.3 dB
quieter than the household's listening level at full volume.

**Lift is bounded to the driver's radiating side of its crossover** (#1809).
The composer passes ``radiating_band_hz`` — solved by
:func:`jasper.active_speaker.branch_chain.radiating_band_hz`, so nothing in
THIS module knows what a crossover is; it is handed a band, like every other
bound it consumes. A driver measured through its own crossover carries that
crossover's rolloff in its curve, and a boost-capable fit reads the rolloff as
a driver deficit and spends gain undoing a filter the same graph emits three
lines earlier. A vocabulary that forbids
boost still enforces the cut-only invariant with an explicit ``raise`` before
returning (not a bare ``assert`` — a hardware-bound safety invariant must
survive ``python -O``; see :func:`fit_driver_linearization`), pinned by a test.

**The SOLVE runs over the same band widened by half an octave** (#2523), which
is a SECOND, looser bound and not the one above. #1809's asymmetry stands
exactly where it was measured: a cut past the handoff is ordinary useful work,
because whatever leaks through still reaches the sum and removing it spends no
headroom. What #1809 did not have to answer, because it predates R10a's
crossover-shaped target, is how far past. A real branch does not follow an
IDEAL crossover into its own deep stopband — it flattens into breakup, leakage
and the noise floor — so graded against one, its stopband reads as tens of dB
of "too loud" that no cut-only cascade of eight filters at 12 dB apiece can
answer, and the greedy search spends slots there anyway. So the objective stops
where the branch stops materially reaching the sum: the declared band widened
by :data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`,
which keeps every shoulder cut #1809 measured and drops the deep-stopband
demand it never looked at. Bins outside are EXCLUDED from the objective, never
given a zeroed target — a zeroed target is still a demand a greedy search can
win on. See :func:`_solve_band_mask`.

**Boost filters AIMED at a band the cloud's positions disagree about are
dropped** (#1967). :attr:`FitVocabulary.boost_excluded_bands_hz` is a
realized-response bound like #1968's stopband guard, but it is applied PER
FILTER and on a RELATIVE criterion: a filter goes when the band lies inside
its own half-gain bandwidth, i.e. when its action region overlaps the band.
Skirt spill from a filter working elsewhere is kept and disclosed, never
refused. Composed the same way as every other bound here: this module is
handed bands, never evidence, and knows nothing about clouds or positions.
What the composer puts there is the narrow, decided case — the cross-position
check positively CONTRADICTED boosting at those bins — never "no evidence was
available", because withholding boost wherever nothing was measured is the
blunt gate the owner's 2026-07-27 ruling rejected. Cuts are again untouched.

**The fit domain is whatever grid the caller's ``EnvelopeCurve`` was
composed on** — :data:`~jasper.active_speaker.linearization_envelope.
DEFAULT_ENVELOPE_GRID_HZ` for every production caller (`compose_envelope`'s
own default), read here as ``envelope.freqs_hz`` rather than re-imported as
a separate constant, so this module can never silently disagree with the
grid the envelope it is fitting against actually used.

**Artifact-02 §6's boost-cap table is SUPERSEDED, not implemented.** The
driver-linearization research (``docs/research/2026-07-23-driver-
linearization/02-engineering-spec.md`` §6) described a boost-capable mode
capped at a global +6 dB and gated by closed-loop achieved-vs-predicted
verification. PR-L5 kept the gate and dropped the cap: the owner's 2026-07-27
ruling is that "a 4 dB natural darkness gets its 4 dB" and that headroom spend
is disclosed rather than limited. The +6 dB figure was a proxy for "do not let
an unverified boost run away", and the delta probe measures the thing that
proxy was standing in for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.correction.peq import design_peq, predicted_response
from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

from .branch_chain import chain_response
from .branch_target import (
    SIGNIFICANT_GAIN_DB,
    STOPBAND_GAIN_MARGIN_OCTAVES,
    BranchTarget,
    octave_scaled,
)
from .linearization_envelope import (
    ENVELOPE_CEILING_SENTINEL_DB,
    EnvelopeCurve,
    ReasonCode,
)

# --------------------------------------------------------------------------- #
# fitting policy constants
# --------------------------------------------------------------------------- #

# Per-filter cut ceiling, dB. Shared by the shelf stage (its own single
# gain) and the peaking loop's per-bin cap array (min'd against the
# envelope's own allowed depth) — design doc "cuts generous (-12 dB, Q<=8)".
PER_FILTER_CUT_CAP_DB: float = 12.0

# The coordinator's ruling (original 6 dB decided 2026-07-23; RAISED to
# 12 dB 2026-07-24 for CD-horn compensation give-back): a bound on TOTAL
# normalization spend across the whole fit — how far below the driver's own
# core-passband peak the fit is allowed to settle. Cut-only slope-flattening
# and the CD-horn continuation stage both "spend" driver sensitivity (every
# dB cut here is a dB of max-SPL headroom the corrected driver gives up);
# left unbounded, a driver with a naturally rolling-off passband could have
# the fit chase that rolloff arbitrarily deep, burning sensitivity for a
# shape the driver was never going to deliver cleanly anyway.
#
# Why 18 (6 → 12 on 2026-07-24, → 18 after that night's JTS3 hardware run):
# the owner's "flat as a table top" directive requires the spend to actually
# REACH the measured deficit. The live JTS3 tweeter measured a 14.2–14.3 dB
# deficit at the reference-tier confidence ceiling (~16.4 kHz), but the 12 dB
# budget capped spend at ~9.2 across both quiet-room runs — the correction was
# budget-bound below the measured trend and the treble still sloped away. At
# 18 the ledger covers it: total ledger = (plateau − target) + spend ≈ 17.3 on
# that rig, inside the budget with margin.
#
# The CD-horn continuation stage (_hf_continuation_stage) realizes the lift in
# the cut domain — it cuts everything BELOW the compensation region by `spend`,
# and the planner's ANCHORED trim give-back (crossover_v2.intervention.plan_linearization)
# returns exactly what the emitted cascade removed, so the top octave lands
# `spend` dB higher RELATIVELY. The spend is a max-SPL LEDGER cost, not a
# listening-level cost: the system's absolute ceiling drops by ~spend, but
# ordinary listening recovers it via the volume knob — and it is disclosed
# (`correction_giveback_db`, `hf_continuation_spend_db`). The literal-boost
# realization that would reclaim the physical L-pad margin instead of spending
# sensitivity is deliberately DEFERRED until the closed-loop verify layer
# (design doc build-order step 2 / PR-E) exists to bound an unverified boost
# claim. This constant is the single knob the owner's listening ladder revisits.
#
# NOTE the spend can now exceed PER_FILTER_CUT_CAP_DB (12): no single filter
# may, so the CD-horn stage clamps its Lowshelf backbone at the per-filter cap
# and lets the peaking residual fit absorb the rest (see _hf_continuation_stage).
#
# Enforcement (see _shelf_stage and _hf_continuation_stage): a stage's spend
# is clamped so the region it corrects never gets pulled more than the
# REMAINING budget below the core-passband peak (`MAX_NORMALIZATION_SPEND_DB
# − (plateau_level_db − target_level_db)`, floored at 0 — the plain
# target-vs-plateau gap the core already spends is charged first).
# `target_level_db` itself (the median used by the shelf and the peaking
# loop) is left UNCLAMPED — a plain median of the trusted core region (see
# _target_and_plateau_db). A clamped stage leaves an honest gap between the
# corrected curve and target; for the CD-horn stage that gap is disclosed as
# `measured_deficit_at_ceiling_db` (the uncapped measured deficit) so partial
# correction is visible, not hidden.
MAX_NORMALIZATION_SPEND_DB: float = 18.0

# Linear-regression slope (dB per octave, over log2(f)) above which the fit
# band is treated as a genuine tilted shelf shape (CD-horn compensation,
# baffle-step) rather than local ripple the peaking loop alone should
# handle. Only a RISING (positive) slope fires the shelf stage — cut-only
# fitting cannot correct a naturally FALLING response (that would need
# boost), so a falling slope is left to the peaking loop / accepted as the
# driver's honest natural rolloff, matching the design doc's "textbook
# slopes are never assumed" backstop.
SHELF_SLOPE_THRESHOLD_DB_PER_OCT: float = 3.0

# Hard cap on filters per driver (shelf + peaking combined) — design doc
# "Fitting policy" via the engineering-spec build-order.
MAX_FILTERS_PER_DRIVER: int = 8

# Per-filter BOOST ceiling, dB (PR-L5) — the mirror of PER_FILTER_CUT_CAP_DB,
# and deliberately the same number.
#
# This is a REALIZATION bound, not a policy cap, which is why it survives the
# owner's "arbitrary gain caps GO" ruling while the total stays uncapped. One
# RBJ biquad asked for +12 dB already has a Q-dependent transition wide enough
# to be doing something other than what the fit drew; past that the emitted
# filter stops being a faithful realization of the requested shape, exactly as
# on the cut side. TOTAL boost remains unbounded because a cascade composes:
# a deeper deficit gets more filters, not one absurd one — the same way the
# CD-horn stage clamps its Lowshelf at the per-filter cap and lets the peaking
# residual absorb the rest.
PER_FILTER_BOOST_CAP_DB: float = 12.0

# A bin below this allowed-depth is treated as "the envelope permits
# nothing here" (float noise / a taper's asymptotic tail rather than a
# real allowance). Matches the value validated against the real N=3
# capture during PR-C scoping.
_ENVELOPE_NONZERO_EPS_DB: float = 0.05

# Below this magnitude a filter is cosmetic (inaudible, wastes a filter
# slot) — mirrors design_peq's own default `min_filter_gain_db`. Kept as a
# LOCAL constant (not imported) because it also gates the shelf stage's own
# worth-adding check, which is this module's logic, not design_peq's; if
# design_peq's default ever changes independently, revisit this mirror.
_MIN_FILTER_GAIN_DB: float = 0.5

_PEAKING_Q_MAX: float = 8.0

# The narrowest a BOOST bell may be. Passed EXPLICITLY at the lift stage's
# ``design_peq`` call even though it equals that function's own default,
# because since #1967 it is load-bearing for a safety property in THIS module
# and an inherited default is not a bound this module controls.
#
# **What depends on it.** :func:`_boost_exclusion_verdicts` drops a boost when
# an excluded band lies inside the filter's own half-gain bandwidth. For a
# peaking biquad that bandwidth is a function of Q alone (pinned by
# ``test_the_action_region_depends_on_q_alone_not_on_how_big_the_boost_is``),
# so the Q floor IS the drop radius:
#
#     Q = 1.0  ->  +/- 0.68 octaves     (the shipped bound)
#     Q = 0.5  ->  +/- 1.25 octaves
#     Q = 0.3  ->  +/- 1.85 octaves
#
# Lowering this widens every drop decision by the same factor and walks the
# bound back toward the whole-cascade bluntness #1967 round 2 was rejected
# for — a boost working an octave and a half away would start being read as
# "aimed at" the band. If a future tuning genuinely needs broader boost
# bells, re-derive the drop criterion in the same PR; do not move this alone.
_PEAKING_Q_MIN: float = 1.0
_PEAKING_FLATNESS_TARGET_DB: float = 1.0

# The RBJ Highshelf's fixed Butterworth Q — mirrors
# jasper.camilla_config_contract.SHELF_Q and jasper.sound.profile._SHELF_Q
# (see this module's top docstring for why it is duplicated rather than
# imported). The APPLY stage spells this SAME number into the emitted shelf's
# CamillaDSP ``q`` field (``camilla_stereo_prefix.emit_filter_spec``), so the
# modeled response this module subtracts during fitting — and the realization
# gate, residual, and VERIFY prediction built on it — is the response the
# speaker actually realizes.
#
# It was NOT, before 2026-07-27: the emitter wrote ``slope: 6.0`` believing
# that was Butterworth. CamillaDSP's Butterworth is ``slope: 12`` (S = 1); at
# ``slope: 6`` the realized Q depends on the shelf's gain and collapses to
# 0.476 at -11 dB. Because every gate in this module evaluated the Butterworth
# shelf, a shelf that missed its design by up to 1.7 dB scored as exact — the
# fit could not see its own realization error. Keep the emitted parameter and
# this constant in lockstep; ``tests/test_sound_peq_response.py`` pins them to
# CamillaDSP's own slope↔Q formula.
_HIGHSHELF_Q: float = 1.0 / math.sqrt(2.0)

# Octave-band centers for the candidate artifact's compact reason summary
# (design doc "UX reason codes" — an octave-band summary, not a per-bin
# dump). Mirrors the PR-C scoping experiment's own diagnostic printout.
_OCTAVE_BAND_CENTERS_HZ: tuple[float, ...] = (
    250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 12000.0, 16000.0, 20000.0,
)

# --------------------------------------------------------------------------- #
# CD-horn compensation stage constants (#1668)
# --------------------------------------------------------------------------- #

# Low bound of the CD-horn continuation stage's compensation/agreement band.
# The measured top-octave deficit is expressed RELATIVE to the driver's
# trusted 4-8 kHz band (owner brief, 2026-07-24), so agreement is checked
# from 4 kHz up to the confidence ceiling — below 4 kHz the deficit is not
# what this stage compensates.
HF_COMPENSATION_BAND_LO_HZ: float = 4_000.0

# Repeat-agreement gate (objective suppression). Per-bin spread (max-min
# across the capture's repeat sweeps, ladder-smoothed) over the compensation
# band [HF_COMPENSATION_BAND_LO_HZ, ceiling] must stay under these limits or
# the stage is suppressed (no filters, reason="repeat_disagreement"). Two
# tiers because measurement noise grows with frequency: 1.0 dB below 10 kHz,
# 2.0 dB in [10 kHz, ceiling]. Sourced from the owner's per-serial-calibrated
# UMIK-2 measurement-uncertainty research (2026-07-24): the stock-cal
# protocol's own uncertainty is ~+/-1.5 dB @12 kHz / +/-2.3 dB @16 kHz, so
# repeats disagreeing by more than these tighter limits are a red flag that
# THIS capture is noisier than the protocol's baseline and its top-octave
# trend should not be trusted enough to correct. This replaces any subjective
# "does the curve look clean" judgment with a measured gate.
HF_AGREEMENT_LIMIT_LOW_DB: float = 1.0
HF_AGREEMENT_LIMIT_HIGH_DB: float = 2.0

# The frequency (Hz) below which HF_AGREEMENT_LIMIT_LOW_DB applies (and at/
# above which HF_AGREEMENT_LIMIT_HIGH_DB applies) within the agreement band.
_HF_AGREEMENT_TIER_SPLIT_HZ: float = 10_000.0

# Minimum sweep occurrences (primary + repeats) the agreement gate needs to
# evaluate reproducibility — the N>=3-total "paired gate" (sigma-seeding
# report finding 5). LOCKSTEP with the planner's
# ``crossover_v2.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES`` (3); kept
# as a local constant rather than imported because that module imports THIS
# one (`intervention` does `from ..linearization_fit import ...`, so the
# reverse import would be a cycle), mirroring this module's other
# lockstep-duplicate constants. Below this the gate returns
# "insufficient_repeats".
_HF_MIN_OCCURRENCES: int = 3

# The closed vocabulary of CD-horn continuation suppression reasons — every
# non-empty ``hf_continuation_suppressed_reason`` a fit can carry. Pinned by a
# test so a new suppression path can't ship an un-enumerated reason string.
HF_SUPPRESSION_REASONS: frozenset[str] = frozenset({
    "insufficient_repeats",
    "repeat_disagreement",
    "fit_quality",
    "no_filter_budget",
})

# Max magnitude error (dB) tolerated between the realized cut-domain cascade
# (lowshelf + peaking cuts) and the desired cut_target over [onset, ceiling].
# Above this the whole stage is suppressed (reason="fit_quality") rather than
# ship a mis-shaped correction — the realized shape, not just its peak, has to
# track the measured inverse. Suppression semantics are unchanged; only the
# threshold moved.
#
# 1.5 → 2.0 (live JTS3 probe, 2026-07-24 night). The gate exists to catch a
# mis-SHAPED correction, and on REAL (ragged) curves it was also catching
# ordinary curve raggedness: runs 4/5 realized a 9.27 dB spend at ~1.3 dB max
# error — passing, but only 0.2 dB from the gate — while an offline spend ladder
# on run-6's own capture put the pass/fail cliff at ~11.9 dB spend. An isolated
# 1.5-2.0 dB excursion at the smoothing scale is measurement texture, not a
# shape failure; the worst mis-shape reachable in review probing measured
# 2.23 dB — still caught, and the spend cap above makes that regime
# unreachable in production anyway. (The original 1.5 mirrored the crossover VERIFY
# tolerance, but that gate judges a SUMMED acoustic prediction against a
# measurement — a different quantity from this one, which judges a modeled
# biquad cascade against its own design target.)
HF_REALIZATION_TOLERANCE_DB: float = 2.0

# Ceiling on the CD-horn spend imposed by the SINGLE-Lowshelf realization, dB —
# independent of, and binding below, the MAX_NORMALIZATION_SPEND_DB ledger
# budget. Measured live on JTS3 2026-07-24 (run-6's capture, probed offline
# through the real fit at a spend ladder): the realization passes the quality
# gate at spend 11.27 (4 filters) and fails from ~11.9 upward. The cliff sits
# just BELOW the per-filter clamp, so raising the ledger budget alone does not
# buy more correction — past it the clamped shelf leaves a wide residual
# plateau that design_peq cannot cover with bells on a real curve, and the whole
# stage suppresses (exactly what run 6 did at spend 14.33). 11.0 leaves margin
# under the measured cliff.
#
# This caps how much lift ONE shelf can deliver, not how much the driver needs:
# the ladder showed spend 11.27 → OBSERVE 12k −0.7 / 16k −2.7, versus spend
# 14.33 → 12k +0.9 / 16k −0.0. The last ~3 dB toward true tabletop requires a
# different REALIZATION, not a bigger number here — either the stacked-shelf
# realization (two cascaded shelves sharing the depth; a contract extension,
# future PR) or the literal-boost realization (post-PR-E, once closed-loop
# verify can bound a boost claim). Raising this constant without one of those
# just re-enters the suppression regime.
HF_SINGLE_SHELF_SPEND_CAP_DB: float = 11.0

# Flatness target for the CD-horn stage's own residual peaking fit — TIGHTER
# than the flattening loop's `_PEAKING_FLATNESS_TARGET_DB` (1.0) because this
# fit tracks a SHAPED target (`cut_target`) and is then judged by
# HF_REALIZATION_TOLERANCE_DB. `design_peq` stops when its band RMS drops under
# this AND no large peak remains; with the loose 1.0 the RMS over the (mostly
# well-matched) fit band diluted the still-unfitted shelf-transition error and
# it stopped early — measured on the live-rig-shaped 14.3 dB deficit: 2 of 7
# slots used, 2.18 dB residual error, suppressed by the realization gate. At
# 0.5 (a third of the gate it must satisfy) the same case uses 5 slots and
# lands at 1.27 dB. Shallower deficits are unaffected (the canonical 11.4 dB
# synthetic fits identically at either value).
_HF_RESIDUAL_FLATNESS_TARGET_DB: float = 0.5

# Max cut (dB) of the "taper" continuation policy's single trailing Highshelf.
# Above the confidence ceiling nothing is measurable, so for breakup-prone /
# unknown driver tops the stage walks the relative lift back DOWN with a
# gentle shelf cut of min(spend/2, this). Capped at 6 dB (owner ruling,
# 2026-07-24) so the taper protects the unseen top without itself becoming a
# large unverified move.
HF_TAPER_MAX_DB: float = 6.0

# Continuation policy above the confidence ceiling, keyed by DECLARED driver
# class — the driver class's ONLY remaining authority over the CD-horn stage
# (owner-confirmed 2026-07-24; sizing is class-blind, from measurement).
# "hold": nothing extra above the ceiling — the cut_target is already 0 there,
# so the relative lift stays constant and smooth by construction. Appropriate
# for drivers whose top is trusted to keep extending (compression horn,
# soft/beryllium/diamond domes, ribbon/AMT). "taper": append one trailing
# Highshelf CUT that walks the lift back down above the band we cannot see —
# for a rising-breakup metal dome, and for an UNKNOWN driver where the
# conservative default is to not project a lift into a top we know nothing
# about. Keys MUST cover DRIVER_CLASSES exactly (pinned by a test) so a new
# declared class can never fall through to an undefined policy.
HF_CONTINUATION_POLICY: Mapping[str, str] = {
    "compression_horn": "hold",
    "soft_dome": "hold",
    "beryllium_diamond_dome": "hold",
    "ribbon_amt": "hold",
    "metal_dome": "taper",
    "unknown": "taper",
}


# --------------------------------------------------------------------------- #
# the allowed vocabulary + the shared level frame (PR-L5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FitVocabulary:
    """What moves this fit is allowed to make — the "allowed vocabulary in" of
    the topology-agnostic fit core (plan PR-L5).

    Kept deliberately small: every field here is a move the fit can MAKE, not
    a fact about the speaker. Way count, driver roles, pad authority, and
    alignment are the composer's business — an active speaker grants per-driver
    channels and this vocabulary per driver; a passive box grants one channel
    and this vocabulary once, on the summed chain. Neither shape is spelled
    anywhere in this module.

    ``allow_boost`` is the evidence gate. The v2 session grants it only when
    the commission will actually run the delta probe, so an unverified boost
    claim cannot reach a driver — which is the whole condition under which the
    engineering spec's boost mode was allowed to exist at all.
    """

    allow_boost: bool = False
    #: Per-filter boost ceiling. TOTAL boost is uncapped by design (owner
    #: ruling); this bounds one biquad's realization, not the correction.
    per_filter_boost_cap_db: float = PER_FILTER_BOOST_CAP_DB
    #: Bands no LIFT filter may be AIMED at (#1967). Enforced per filter on
    #: the emitted response — a boost is dropped when the band falls inside
    #: its own half-gain bandwidth — rather than on the request, and never as
    #: a whole-cascade veto; see :func:`_boost_exclusion_verdicts` for the
    #: criterion and :func:`_lift_stage` for the measured pathologies of the
    #: two alternatives. Cuts are untouched: this narrows one move, which is
    #: why it lives on the vocabulary rather than in the envelope's
    #: ``allowed_depth_db`` — that array is direction-agnostic and zeroing it
    #: would forbid a legitimate cut at the same bins.
    #:
    #: The composer supplies bands its cross-position evidence positively
    #: CONTRADICTS boosting at; empty (the default) is "nothing contradicted",
    #: which is every caller before #1967 exactly. This is not a
    #: "no evidence" list — absence of evidence leaves the band in, because
    #: withholding boost wherever nothing was measured is the blunt gate the
    #: owner's ruling rejected.
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_boost": self.allow_boost,
            "per_filter_boost_cap_db": self.per_filter_boost_cap_db,
            "boost_excluded_bands_hz": [
                [float(lo), float(hi)] for lo, hi in self.boost_excluded_bands_hz
            ],
        }


#: The pre-PR-L5 posture, and the default: cuts only. Every existing caller
#: that does not pass a vocabulary gets exactly the fit it got before this
#: capability existed, byte for byte.
CUT_ONLY_VOCABULARY = FitVocabulary()


@dataclass(frozen=True)
class BoostExclusionDrop:
    """One boost filter the #1967 bound removed, and the arithmetic that
    removed it — so a reader can re-derive the decision instead of trusting
    it.

    ``realized_in_band_db`` is this filter's OWN realized magnitude at its
    strongest point inside ``band_hz``; it was dropped because that reached
    at least ``gain_db / 2``, i.e. the band lies inside the filter's own
    half-gain bandwidth.
    """

    band_hz: tuple[float, float]
    freq_hz: float
    q: float
    gain_db: float
    realized_in_band_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            "freq_hz": self.freq_hz,
            "q": self.q,
            "gain_db": self.gain_db,
            "realized_in_band_db": self.realized_in_band_db,
        }


@dataclass(frozen=True)
class BoostExclusionResidual:
    """What the emitted cascade still puts inside one excluded band after the
    drops — the ACCEPTED, disclosed remainder.

    Skirt tail by construction: no surviving filter's action region overlaps
    the band, or it would have been dropped. Refusing on this number would be
    refusing a correction on the strength of a model; the post-apply sweep
    measures the reality instead.
    """

    band_hz: tuple[float, float]
    realized_max_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            "realized_max_db": self.realized_max_db,
        }


@dataclass(frozen=True)
class BoostEvidenceDrop:
    """One boost filter the MEASURED-TARGET bound removed (#2599), and the
    arithmetic that removed it.

    ``action_band_hz`` is the span of this filter's own half-gain bandwidth
    that the fit makes a claim over; ``measured_excess_db`` is how far the
    branch's own smoothed MEASURED response sits ABOVE the fit's target at
    the least-hot bin in that span. It was dropped because that number is
    ``>= 0`` everywhere it acts: the measurement says there is no deficit
    anywhere this filter would add level.
    """

    freq_hz: float
    q: float
    gain_db: float
    action_band_hz: tuple[float, float]
    measured_excess_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "freq_hz": self.freq_hz,
            "q": self.q,
            "gain_db": self.gain_db,
            "action_band_hz": [self.action_band_hz[0], self.action_band_hz[1]],
            "measured_excess_db": self.measured_excess_db,
        }


@dataclass(frozen=True)
class BlindZonePlacement:
    """One peaking filter the fit CENTRED in a span no branch's own
    measurement covers (#2599). A disclosure, not a refusal — the filter ships.

    ``blind_band_hz`` is the hole it landed in —
    :func:`measurement_hole_bands_hz` over the session's per-branch core
    bands. ``measured_excess_db`` is ``smoothed - target`` at that centre:
    what THIS branch believed it was removing, which is the number that makes
    the record actionable. A large excess is a real driver feature the
    crossover is not hiding (act on Fc or order); a small one, like the 1.7577
    dB cut at 1404.4032 Hz on the 2026-08-16 jts3 run, is the fit shaping a
    blend it cannot see. See :func:`_blind_zone_placements` for why this layer
    reports rather than refuses.
    """

    freq_hz: float
    q: float
    gain_db: float
    blind_band_hz: tuple[float, float]
    measured_excess_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "freq_hz": self.freq_hz,
            "q": self.q,
            "gain_db": self.gain_db,
            "blind_band_hz": [self.blind_band_hz[0], self.blind_band_hz[1]],
            "measured_excess_db": self.measured_excess_db,
        }


def _ladder_smooth(grid_hz: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    """The design doc's smoothing ladder: 1/6 oct below 4 kHz, 1/3 oct
    4-10 kHz, 1/2 oct at/above 10 kHz.

    PARITY DUPLICATE of
    ``jasper.active_speaker.linearization_envelope._ladder_smooth``
    (module-private there, so not imported — see this module's top
    docstring). LOCKSTEP REQUIREMENT: any change to that helper's
    breakpoints/fractions must be mirrored here, or this fit engine and the
    envelope it fits against disagree about what "smoothed" means.
    ``tests/test_active_speaker_linearization_fit.py`` pins the two
    functions numerically identical.
    """
    fine = smooth_fractional_octave(grid_hz, magnitude_db, fraction=6)
    mid = smooth_fractional_octave(grid_hz, magnitude_db, fraction=3)
    coarse = smooth_fractional_octave(grid_hz, magnitude_db, fraction=2)
    return np.where(grid_hz < 4_000.0, fine, np.where(grid_hz < 10_000.0, mid, coarse))


def _highshelf_response_db(
    freqs_hz: np.ndarray, corner_hz: float, gain_db: float, q: float,
) -> np.ndarray:
    """RBJ Audio EQ Cookbook Highshelf magnitude response, in dB, evaluated
    at ``freqs_hz`` for a filter designed at ``corner_hz``/``gain_db``/``q``.

    Mirrors ``jasper.sound.profile``'s ``_biquad_coeffs``/``_filter_response_db``
    Highshelf math (module-private there — see this module's top docstring
    for why it is duplicated rather than imported) — the same digital
    biquad family CamillaDSP realizes, at
    :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`. At ``gain_db=0``
    this is identically 0 dB everywhere (unity); at ``freq==corner_hz`` the
    response is ``gain_db/2`` (the RBJ shelf's well-known half-gain-at-corner
    property — pinned by a test against ``jasper.sound.profile``'s own
    fixture-anchored behavior in ``test_sound_peq_response.py``).
    """
    fs = float(RESPONSE_SAMPLE_RATE_HZ)
    w0 = 2.0 * math.pi * max(float(corner_hz), 1e-6) / fs
    cw0, sw0 = math.cos(w0), math.sin(w0)
    amp = 10.0 ** (float(gain_db) / 40.0)
    alpha = sw0 / (2.0 * float(q))
    beta = 2.0 * math.sqrt(amp) * alpha
    b0 = amp * ((amp + 1) + (amp - 1) * cw0 + beta)
    b1 = -2.0 * amp * ((amp - 1) + (amp + 1) * cw0)
    b2 = amp * ((amp + 1) + (amp - 1) * cw0 - beta)
    a0 = (amp + 1) - (amp - 1) * cw0 + beta
    a1 = 2.0 * ((amp - 1) - (amp + 1) * cw0)
    a2 = (amp + 1) - (amp - 1) * cw0 - beta

    f = np.asarray(freqs_hz, dtype=np.float64)
    w = 2.0 * np.pi * np.maximum(f, 1e-6) / fs
    c1, s1 = np.cos(w), np.sin(w)
    c2, s2 = np.cos(2.0 * w), np.sin(2.0 * w)
    num_re = b0 + b1 * c1 + b2 * c2
    num_im = -(b1 * s1 + b2 * s2)
    den_re = a0 + a1 * c1 + a2 * c2
    den_im = -(a1 * s1 + a2 * s2)
    num = num_re * num_re + num_im * num_im
    den = den_re * den_re + den_im * den_im
    mag2 = np.divide(num, den, out=np.zeros_like(num), where=den > 0.0)
    return 10.0 * np.log10(np.maximum(mag2, 1e-12))


@dataclass(frozen=True)
class LinearizationFilter:
    """One filter in a :class:`LinearizationFit` — a plain, JSON-safe record
    (not :class:`jasper.correction.peq.PEQ`, which has no ``biquad_type`` and
    is always implicitly Peaking).
    """

    biquad_type: str  # "Peaking" | "Highshelf" | "Lowshelf"
    freq: float
    q: float
    # dB; may be positive, up to PER_FILTER_BOOST_CAP_DB -- the cut-only
    # invariant ended at PR-L5. Pinned by
    # tests/test_active_speaker_linearization_emission.py::test_linearization_rejects_boost_above_the_per_filter_cap
    # and ::test_linearization_boost_is_accepted_and_absorbed_by_baseline_headroom.
    gain: float

    def to_dict(self) -> dict[str, float | str]:
        return {
            "biquad_type": self.biquad_type,
            "freq": self.freq,
            "q": self.q,
            "gain": self.gain,
        }


#: The persisted key naming WHICH MICROPHONE measured the round this entry
#: came from.
#:
#: A named constant rather than a literal because it is the one key on a
#: persisted linearization entry that a SECOND writer legitimately emits:
#: :func:`~jasper.active_speaker.crossover_v2.driver_prescription.driver_prescription_to_candidate_fields`
#: carries it forward onto a prescribed branch, because replacing a role's
#: filters does not change which microphone measured. Its reader is
#: ``CrossoverV2Session._mic_trust_ceiling_hz`` (#2649), which decides where the
#: delta probe may grade; a spelling drift between the three would silently
#: remove that ceiling rather than fail.
MIC_TIER_FIELD = "mic_tier"


@dataclass(frozen=True)
class LinearizationFit:
    """One driver's fitted linearization — the Layer-1a artifact.

    ``fit_band_hz == (0.0, 0.0)`` signals no fit was attempted (the
    envelope allowed correction nowhere — e.g. genuinely no in-band
    evidence); ``filters`` is empty in that case. ``target_level_db`` and
    ``reason_summary`` still carry honest values in that degenerate case
    (target 0.0, reason summary reflecting the envelope's own out-of-band
    verdicts).

    ``verify_band_hz``/``verify_residual_rms_db``/``verify_residual_max_db``
    and ``observe_octave_summary`` (#1668 PR-D) are the Layer-1a honesty
    ladder's remaining two levels (design doc "three honesty levels": fit /
    verify / observe). ``fit_band_hz``/``residual_*_db`` above are the FIT
    claim — accuracy strictly inside the envelope-allowed, adaptively-trimmed
    band. VERIFY claims the SAME residual math roughly an octave past the
    fit band's own top (``[fit_lo_hz, min(2*fit_hi_hz, grid_top_hz)]``), so a
    fit that only "worked" right at its own edge shows up here even when FIT
    itself looks clean. OBSERVE is the disclosure layer: per-octave
    achieved-vs-target magnitude all the way to the grid's own top (20 kHz on
    the production grid) — "the top octave appears in the technical
    disclosure as the driver's measured natural response, never as a
    pass/fail." All four are REPORT-ONLY in this PR; nothing gates on them
    yet (design doc build-order step 2, closed-loop verify, is a later PR).

    **Every one of those numbers is measured on the REALIZED cascade** (R10b;
    first-principles panel CC-2(b),
    ``captures/first-principles-panel-20260731/objective-verdict.md``) — the
    measurement plus :func:`complex_correction_response`, the exact RBJ biquads
    CamillaDSP emits — never the Lorentzian bell the peaking search uses
    internally to pick its next peak. Same rule for
    :attr:`correction_giveback_db`. Pinned by
    ``test_reported_residual_grades_the_realized_biquad_not_the_lorentzian``.

    **That is the CURVE being measured, not the ruler it is measured against —
    issue #2013.** The residual is ``working_db - frame_target_db``, and the
    FRAME still carries one pre-seam term: ``frame_target_db = target_curve_db -
    hf.spend_db``, where ``hf.spend_db`` is sized inside
    :func:`_hf_continuation_stage` against a ``working_db`` the seam has not
    rebuilt yet. :attr:`measured_deficit_at_ceiling_db` comes from the same
    place. Measured on the banked 2026-07-30 JTS3 session
    (``captures/r10b-alignment-20260801/lorentzian_gap_probe.py``,
    ``exact_fold_counterfactual``): the spend moves 0.143 dB under an exact fold
    and the committed trim up to 0.162 dB — more than the seam itself moves. So
    do not read the paragraph above as "no Lorentzian reaches any reported
    number". It reaches the frame; #2013 owns closing it.

    **All three ladder levels are FIT DIAGNOSTICS, and the flat-linearization
    plan's PR-5 fixed how they are labeled downstream.** Every one of them is
    computed per-driver, on this fit's own envelope grid, from the single
    design-axis MEASURE capture. None of them is the flat-linearization spec
    claim — that is graded on the spatially-combined cloud curve by
    :func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`, over the spec
    bands, with interference-flagged bins excluded. The two answer different
    questions on different curves and will legitimately disagree; the
    household-facing surfaces name which is which
    (``crossover_envelope_v2._linearization_octave_rows`` vs
    ``_flatness_details_lines``).
    """

    role: str
    filters: tuple[LinearizationFilter, ...]
    fit_band_hz: tuple[float, float]
    target_level_db: float
    residual_rms_db: float
    residual_max_db: float
    reason_summary: Mapping[str, str]
    mic_tier: str
    driver_class: str
    n_repeats: int
    verify_band_hz: tuple[float, float] = (0.0, 0.0)
    verify_residual_rms_db: float = 0.0
    verify_residual_max_db: float = 0.0
    observe_octave_summary: Mapping[str, float] = field(default_factory=dict)
    # CD-horn compensation stage (#1668). All default to the zeroed/empty
    # "did-not-fire" state so every driver/session that never runs the stage
    # (woofers, mids, any driver whose fit band doesn't reach the confidence
    # ceiling) serializes byte-identically to before this stage existed.
    # ``hf_continuation_policy`` is "hold"/"taper" only when the stage fired;
    # ``hf_continuation_suppressed_reason`` is set only when an objective gate
    # (repeat disagreement / insufficient repeats / fit quality) suppressed it;
    # ``measured_deficit_at_ceiling_db`` reports the UNCAPPED measured deficit
    # at the ceiling (before the normalization-budget cap) so a partially-
    # corrected top octave (budget bound the spend below the full deficit) is
    # visible rather than silently rounded away.
    hf_continuation_spend_db: float = 0.0
    hf_continuation_ceiling_hz: float = 0.0
    hf_continuation_policy: str = ""
    hf_continuation_suppressed_reason: str = ""
    measured_deficit_at_ceiling_db: float = 0.0
    # How much LEVEL this driver's correction removed from its own reference
    # (core) band, POSITIVE dB — the MEASURED before-vs-after power-domain band
    # average over the ``_core_or_fallback_mask`` region (pre-correction curve
    # minus post-correction curve). Exact by definition of the quantity being
    # restored: it is the level change of the very band whose level the anchor
    # puts back. (Averaging the CORRECTION alone would instead be
    # power-domain-approximate — exact only for a flat core, up to ~1.1 dB
    # under-return on a 12 dB-tilted woofer-shaped core.)
    #
    # This is the SSOT for the AUDIBLE-BAND give-back: what this branch's own
    # correction removed across the driver's own core band, returning that band
    # to its pre-correction system level.
    #
    # **It does NOT place the trim, and has not since the band-mismatch fix.**
    # ``crossover_v2.intervention.plan_linearization`` anchors the trim on a
    # give-back measured over ``branch_level_bands_hz`` — the bands that solved
    # the raw trim and that grade the committed pair — because a give-back spent
    # against a trim must be measured in that trim's frame. This number is
    # published beside it as ``core_band_giveback_db`` and answers the
    # audible-band question instead. On a driver whose correction sits mostly
    # outside the graded band the two legitimately diverge, and using this one
    # to place the trim shipped the jts3 horn tweeter 3.67 dB hot. Full
    # account: that module's anchor block.
    #
    # (The size of that divergence is INFERRED, not banked. What the 2026-08-19
    # journal recorded is this number, 6.656 dB, and the 3.671 dB of committed
    # inter-driver error it produced. The level-band give-back that would have
    # been read instead did not exist on that box — this change is what
    # introduces the estimator — so no measurement of it is available for that
    # session, and any figure quoted for it is arithmetic on the other two
    # rather than a reading. Do not confuse it with the woofer's own core-band
    # give-back of 2.985 dB from the same line, which is a different driver
    # answering a different question and merely lands nearby.)
    #
    # (Historical note, because it is the reason this band was chosen: the
    # pre-PR-L3 alternative averaged over the shared CROSSOVER OVERLAP band and
    # returned only 5.81 dB of a 9.27 dB spend on JTS3 2026-07-24, because the
    # tweeter's LR4 skirt power-weights that average toward the least-cut region
    # and the shelf's wide RBJ transition is not at full depth there. PR-L3
    # deleted that frame — the estimator now reads each branch on its own side
    # of Fc — so the objection does not carry to the level-band route above.)
    #
    # Note the realization fit-quality gate bounds the correction's SHAPE only
    # over [onset, ceiling]; below the onset the realized cascade may diverge
    # from cut_target (e.g. a clamped shelf the residual only partly absorbs).
    # That is safe for THIS number because it is a MEASURED before-vs-after
    # delta rather than a prediction: an under-realized plateau shrinks the
    # measured lift, and this number simply reports the smaller figure. (It
    # used to say "safe because the anchor consumes this measured delta" — the
    # anchor no longer consumes it; the safety came from the measurement, not
    # from the consumer, so the argument survives losing that clause.)
    #
    # Computed for EVERY fit that emitted filters (0.0 when none), so a woofer
    # carrying only flattening cuts still reports its own audible-band figure
    # rather than a gap. When the CD-horn stage fires this reads ≈ spend + the
    # flattening peaks' own in-band share.
    correction_giveback_db: float = 0.0
    # --- PR-L5 disclosure, #1808 charge ----------------------------------
    # "This correction costs N dB of maximum level" — the realized peak of the
    # branch chain this fit is emitted into (``crossover ⊗ linearization ⊗
    # trim``) plus ``branch_chain.HEADROOM_MARGIN_DB``, which is exactly the
    # quantity the emitter CHARGES to ``active_baseline_headroom`` (the same
    # gain room-correction boost already rides; see
    # ``camilla_yaml.linearization_headroom_db``). One number: what a
    # household is told the correction costs is what the speaker gives up.
    #
    # **Stamped by the composer, not computed here.** A correction's cost is a
    # property of the chain it is emitted into — the crossover that follows it
    # and the trim that follows that — and this topology-agnostic core
    # deliberately knows neither (see the module docstring's "the allowed
    # vocabulary is an INPUT" rule). ``crossover_v2.intervention.plan_linearization``
    # fills it through :func:`jasper.active_speaker.branch_chain.
    # branch_headroom_db` once the trim is resolved, using the same function
    # the emitter charges with. A fit evaluated with no branch (a direct
    # caller, a test) honestly reports 0.0: no branch, no charge.
    #
    # It used to be the SUM of this fit's positive filter gains. That was a
    # loose upper bound on the same physical quantity and, on the 2026-07-28
    # JTS3 profile, a 5.6x one — 22.458 dB charged against a +4.00 dB realized
    # peak, most of it paying for two boosts the woofer's own crossover then
    # attenuated by 13.3 and 7.8 dB. The owner's ruling (#1808): corrections
    # must never stack invisible headroom.
    # 0.0 for every cut-only fit — which is every fit before PR-L5.
    headroom_cost_db: float = 0.0
    # The lift the boost vocabulary was asked for and what it delivered, in
    # dB, over the fit band — non-zero only when the lift stage fired.
    # ``lift_from_reduced_cuts_db`` is the share bought by SHRINKING this
    # fit's own cuts rather than by adding gain (the first-class "reduce our
    # own cuts" operation); it is free, and a large share of it is the sign
    # of a healthy correction.
    lift_requested_db: float = 0.0
    lift_from_reduced_cuts_db: float = 0.0
    lift_from_boost_db: float = 0.0
    lift_suppressed_reason: str = ""
    # #1967's boost-evidence bound, disclosed at the two levels it decides at.
    # ``lift_boost_excluded_drops`` is one record per boost filter REMOVED
    # because its own action region overlapped a contradicted band;
    # ``lift_boost_excluded_residual`` is what the surviving cascade still
    # puts inside each of those bands, which is accepted skirt rather than a
    # refusal. Both empty for every caller that supplies no excluded bands,
    # which is every caller before #1967. A whole-lift refusal (every boost
    # dropped) additionally sets ``lift_suppressed_reason``; a partial one
    # deliberately does NOT, because a lift did happen.
    lift_boost_excluded_drops: tuple[BoostExclusionDrop, ...] = ()
    lift_boost_excluded_residual: tuple[BoostExclusionResidual, ...] = ()
    # #2599's two measured-evidence bounds, disclosed the same way #1967's is.
    #
    # ``lift_boost_evidence_drops`` is one record per boost filter removed
    # because the branch's own MEASURED response was already at or above
    # target everywhere that filter acts — the bound that closed the
    # ceiling-sentinel hole in the lift stage's realization gate. A whole-lift
    # refusal (every boost dropped) additionally sets
    # ``lift_suppressed_reason`` to ``"boost_above_measured_target"``; a
    # partial one deliberately does not, for #1967's reason exactly (a lift
    # did happen).
    #
    # ``blind_zone_placements`` is one record per EMITTED Peaking filter whose
    # centre lands inside a span no branch's own capture covers — every stage's
    # output, cuts and lift boosts alike, read off the final cascade so the
    # "no Peaking filter is centred in a hole without being named" universal
    # holds by construction. Unlike the drops above it removes nothing: the
    # filter ships and is NAMED, which is the #2600 disclosure class. Empty for
    # every caller that declares no such span, which is every one-way box and
    # every caller before #2599.
    lift_boost_evidence_drops: tuple[BoostEvidenceDrop, ...] = ()
    blind_zone_placements: tuple[BlindZonePlacement, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "filters": [f.to_dict() for f in self.filters],
            "fit_band_hz": list(self.fit_band_hz),
            "target_level_db": self.target_level_db,
            "residual_rms_db": self.residual_rms_db,
            "residual_max_db": self.residual_max_db,
            "reason_summary": dict(self.reason_summary),
            MIC_TIER_FIELD: self.mic_tier,
            "driver_class": self.driver_class,
            "n_repeats": self.n_repeats,
            "verify_band_hz": list(self.verify_band_hz),
            "verify_residual_rms_db": self.verify_residual_rms_db,
            "verify_residual_max_db": self.verify_residual_max_db,
            "observe_octave_summary": dict(self.observe_octave_summary),
            "hf_continuation_spend_db": self.hf_continuation_spend_db,
            "hf_continuation_ceiling_hz": self.hf_continuation_ceiling_hz,
            "hf_continuation_policy": self.hf_continuation_policy,
            "hf_continuation_suppressed_reason": self.hf_continuation_suppressed_reason,
            "measured_deficit_at_ceiling_db": self.measured_deficit_at_ceiling_db,
            "correction_giveback_db": self.correction_giveback_db,
            "headroom_cost_db": self.headroom_cost_db,
            "lift_requested_db": self.lift_requested_db,
            "lift_from_reduced_cuts_db": self.lift_from_reduced_cuts_db,
            "lift_from_boost_db": self.lift_from_boost_db,
            "lift_suppressed_reason": self.lift_suppressed_reason,
            "lift_boost_excluded_drops": [
                d.to_dict() for d in self.lift_boost_excluded_drops
            ],
            "lift_boost_excluded_residual": [
                r.to_dict() for r in self.lift_boost_excluded_residual
            ],
            "lift_boost_evidence_drops": [
                d.to_dict() for d in self.lift_boost_evidence_drops
            ],
            "blind_zone_placements": [
                p.to_dict() for p in self.blind_zone_placements
            ],
        }


def complex_correction_response(
    filters: Sequence[LinearizationFilter], freqs_hz: np.ndarray,
) -> np.ndarray:
    """The COMPLEX (minimum-phase) response the emitted filters apply across
    ``freqs_hz``.

    The emitted CamillaDSP biquads are minimum-phase and rotate phase near
    their corners. When a correction is applied to a driver branch that is then
    SUMMED with the other branch through the crossover, that summation is
    phase-dominated, so modeling the correction as a zero-phase magnitude scale
    (``W * 10**(magnitude_db/20)``) mispredicts the summed response — and,
    because it perturbs magnitude without the compensating phase, can land the
    prediction FURTHER from the true summation than omitting the filters
    entirely. Measured on JTS3 (issue #1667): against the same VERIFY capture,
    the zero-phase magnitude model mistracked the summation by ~2.0 dB (WORSE
    than the ~1.7 dB of a no-correction model), where this complex model tracks
    it to ~0.5 dB. So the planner's linearized-branch model
    (:func:`jasper.active_speaker.crossover_v2.intervention.plan_linearization`
    — the trim re-solve, the ripple-optimal scan, and the
    persisted VERIFY prediction) multiplies each branch by THIS, not a
    magnitude scale. There is no zero-phase branch-correction path.

    Every entry — Peaking and Highshelf alike — is the exact RBJ biquad
    CamillaDSP realizes, via :func:`jasper.sound.profile._filter_response_complex`
    (the complex twin of the parity-pinned ``_filter_response_db``, sharing the
    ``_biquad_coeffs`` SSOT). It is IMPORTED rather than re-derived — unlike
    this module's ``_highshelf_response_db`` magnitude duplicate — precisely so
    the phase and magnitude of the applied correction can never silently
    disagree with the emitted graph:
    ``abs(complex_correction_response(filters, f))`` equals
    ``10**(sum of jasper.sound.profile._filter_response_db over filters / 20)``
    bin-for-bin (pinned by a magnitude-consistency test). Callers apply it in
    the LINEAR domain: ``W_lin = W * complex_correction_response(...)``.

    A thin role adapter since #1808: the cascade evaluation itself is
    :func:`jasper.active_speaker.branch_chain.chain_response`, shared with the
    emitter's headroom charge and the runtime contract's proof so those three
    can never evaluate the same filters differently. This function only
    reduces :class:`LinearizationFilter` records to the plain biquad mappings
    that evaluator speaks.
    """
    return chain_response([f.to_dict() for f in filters], freqs_hz)


def linearization_filters_by_role(
    linearization_mapping: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Reduce a persisted ``{role: LinearizationFit.to_dict()}`` mapping down
    to the emitter's own reduced input shape: ``{role: [filter_dict, ...]}``.

    Shared by the two RICH-candidate call sites that thread a persisted
    linearization result into
    :func:`jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config`
    (#1668 PR-D) — ``measured_crossover_candidate.compile_candidate_config``
    and ``baseline_profile.build_baseline_profile_candidate`` — so the
    reduction is defined once rather than twice.

    ``baseline_profile.recompose_applied_baseline_yaml`` deliberately does
    NOT call this helper. Its snapshot's ``"linearization"`` key is already
    in this function's OUTPUT shape (``build_baseline_profile_candidate``
    is what wrote it), not this function's INPUT shape — calling this
    helper on an already-reduced mapping silently returns ``{}`` for every
    role (each value is a ``list``, which fails the ``isinstance(fit,
    Mapping)`` check below, not an error). recompose re-validates the
    already-reduced shape inline instead, era-tolerantly. Do not
    "consolidate" that seam onto this helper — see
    ``test_linearization_filters_by_role_on_already_reduced_shape_is_empty``
    in ``tests/test_active_speaker_linearization_fit.py`` for the pinned trap.

    ``linearization_mapping`` is whatever a rich candidate carries under its
    ``"linearization"`` key: era-tolerant absence is the caller's job (this
    function treats a missing/malformed role or filter list as simply not
    present, matching "no linearization was fit" rather than raising).

    Defensive, not authoritative: this only reshapes trusted-enough
    persisted data. The emitter's own ``_validated_linearization`` is the
    fail-closed gate that actually enforces shape/safety on what reaches
    CamillaDSP.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for role, fit in (linearization_mapping or {}).items():
        if not isinstance(fit, Mapping):
            continue
        filters = fit.get("filters")
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            continue
        out[str(role)] = [
            dict(entry) for entry in filters if isinstance(entry, Mapping)
        ]
    return out


# --- what a stored ``headroom_cost_db`` MEANS, per era (#1808) -------------
#
# The charge's derivation changed: it was the SUM of a fit's positive filter
# gains, and #1808 made it the realized peak of the branch chain the fit is
# emitted into. On the 2026-07-28 JTS3 profile the two disagree by 5.6x —
# 22.458 dB against a +4.00 dB realized peak — so the SAME correction discloses
# an order-of-magnitude different level cost depending on which build stamped
# it. ``docs/linearization-integrity-plan.md`` ("Cross-era disclosure") rules
# that the stamp is NOT re-derived on load, deliberately: it is a record of
# what that graph was emitted with, and a recommission replaces it.
#
# That ruling is what makes this vocabulary necessary. A number that cannot be
# re-derived and cannot be trusted across eras must travel WITH the era it was
# stamped in, or a household reads 22.5 dB for a correction that costs 5.
#
# The era is recorded, never inferred. Nothing on a persisted
# ``LinearizationFit`` distinguishes the two rules — #1808 changed how the
# field is computed, not the field set (its nearest neighbours, the ``lift_*``
# family, predate it) — so sniffing would be a guess dressed as a fact. A
# candidate serialized by THIS build carries REALIZED_PEAK because the fits
# inside it were stamped by this build's composer; a candidate persisted by an
# older build carries no stamp at all, and UNKNOWN is the honest reading of
# that absence. Absent-means-unknown, never a default — the same rule ``tier``
# and ``echo_band_provenance`` already carry.
#
# **#2758 opened a THIRD era, and its direction is the new one.** The grid the
# realized peak is evaluated on now spans the whole domain the peak is taken
# over rather than 20 Hz - 20 kHz, so a stamp made under the narrower grid can
# read SMALLER than re-emitting the identical filters charges today —
# 1.8596 dB stamped against 7.8305 dB charged, on the cascade #2758 was filed
# for. Every era before this one only ever over-stated (the sum-of-positives
# rule was a loose upper bound), which is why ``sections_by_role``'s docstring
# can still call "disclosure smaller than charge" the impossible direction FOR
# THE DERIVATION IT DESCRIBES — the role -> sections map — and why that
# sentence is not a claim about cross-era stamps.
#
# Reachable rather than theoretical: the republish path stamps
# ``headroom_cost_basis`` unconditionally, so a candidate reopened and
# re-disclosed after the deploy carries a CURRENT-era basis beside a per-branch
# number stamped under the old grid. The basis is what lets a reader tell them
# apart, which is why the widening mints a value instead of riding the old one.
HEADROOM_COST_BASIS_REALIZED_PEAK = "realized_peak"
HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN = "realized_peak_full_domain"
HEADROOM_COST_BASIS_UNKNOWN = "unknown"


def worst_headroom_cost_db(linearization_mapping: Mapping[str, Any]) -> float:
    """The max-level cost of a whole correction, dB — the WORST branch's
    :attr:`LinearizationFit.headroom_cost_db` (PR-L5).

    Worst branch and not the sum across branches, matching
    ``camilla_yaml.linearization_headroom_db``'s own rule: the driver chains
    run in PARALLEL after the split, so no single sample path ever sees two
    branches' boosts and the graph gives up the largest one. (That
    adjudication is unchanged by #1808 — what changed is the per-branch number
    this maxes over, from a sum of positive gains to the branch chain's
    realized peak.)

    Takes a persisted ``{role: LinearizationFit.to_dict()}`` mapping — the
    shape a candidate carries under its ``"linearization"`` key, after a JSON
    round-trip — so it is defensive in the same way
    :func:`linearization_filters_by_role` is: a malformed or era-older entry is
    skipped rather than raising. Returns 0.0 when nothing was boosted or
    nothing was fitted.

    Defined here, once, because BOTH the session's own candidate payload and
    the web layer's browser-visible ``_candidate_summary`` disclose this
    number, and two reducers for one household-facing figure is exactly the
    drift this ladder exists to remove.
    """
    worst = 0.0
    for fit in (linearization_mapping or {}).values():
        if not isinstance(fit, Mapping):
            continue
        cost = fit.get("headroom_cost_db")
        if isinstance(cost, (int, float)) and math.isfinite(float(cost)):
            worst = max(worst, float(cost))
    return worst


def _power_band_average_db(magnitude_db: np.ndarray, mask: np.ndarray) -> float:
    """Power-domain band average of ``magnitude_db`` over ``mask``:
    ``10*log10(mean(10**(dB/10)))``.

    PARITY DUPLICATE of ``jasper.audio_measurement.program_analysis.
    _band_average_db``'s averaging semantics (module-private there — see this
    module's top docstring for the no-cross-module-private-imports convention),
    evaluated against a boolean mask on this module's own fit grid rather than
    a (lo, hi) frequency pair. LOCKSTEP REQUIREMENT: this MUST stay the same
    power-domain mean the trim solver uses, so that
    :attr:`LinearizationFit.correction_giveback_db` and the trim frame remain
    directly comparable quantities (a linear-dB mean here would read ~0.3 dB
    different on a non-flat correction).

    **The reason this requirement exists is now enforced one layer up, and
    completely.** Its original wording said the domains must match or "the
    anchored trim would systematically mis-level the branch" — correct, and it
    guarded ~0.3 dB of averaging-domain error while leaving the BAND unmatched,
    which cost 3.67 dB on a horn tweeter (jts3, 2026-08-19). The anchor no
    longer consumes this number: it measures its own give-back over
    ``branch_level_bands_hz`` with the trim solver's own estimator, so the
    domain, the band, and the estimator all match by construction rather than
    by a comment asking them to. This requirement is kept anyway — the two
    give-backs are published side by side and a reader compares them, which is
    only meaningful while they share an averaging domain.

    Returns 0.0 for an empty mask (no band to average — an honest no-op).
    """
    if not mask.any():
        return 0.0
    power = np.power(10.0, magnitude_db[mask] / 10.0)
    return 10.0 * math.log10(max(float(np.mean(power)), 1e-12))


def _octave_band_reason_summary(envelope: EnvelopeCurve) -> dict[str, str]:
    grid = envelope.freqs_hz
    out: dict[str, str] = {}
    for center in _OCTAVE_BAND_CENTERS_HZ:
        if center < grid[0] or center > grid[-1]:
            continue
        idx = int(np.argmin(np.abs(grid - center)))
        out[str(int(center))] = envelope.reason[idx].value
    return out


def _empty_fit(envelope: EnvelopeCurve) -> LinearizationFit:
    return LinearizationFit(
        role=envelope.role,
        filters=(),
        fit_band_hz=(0.0, 0.0),
        target_level_db=0.0,
        residual_rms_db=0.0,
        residual_max_db=0.0,
        reason_summary=_octave_band_reason_summary(envelope),
        mic_tier=envelope.mic_tier,
        driver_class=envelope.driver_class,
        n_repeats=envelope.n_repeats,
        # Honesty-ladder levels 2/3 (#1668 PR-D) are degenerate placeholders
        # here too, exactly like fit_band_hz/target_level_db/residual_*_db
        # above -- no fit was attempted (the envelope allows correction
        # nowhere), so there is nothing to verify and observe_octave_summary
        # would have no honest target to compare against.
        verify_band_hz=(0.0, 0.0),
        verify_residual_rms_db=0.0,
        verify_residual_max_db=0.0,
        observe_octave_summary={},
    )


def _verify_band_and_residual(
    grid_hz: np.ndarray,
    working_db: np.ndarray,
    target_curve_db: np.ndarray,
    fit_lo_hz: float,
    fit_hi_hz: float,
) -> tuple[tuple[float, float], float, float]:
    """The honesty ladder's VERIFY level: the SAME residual math the fit
    claim itself uses (post-filter ``working_db`` vs the fit's own target),
    evaluated over a band extending roughly an octave
    PAST the fit band's own top — ``[fit_lo_hz, min(2*fit_hi_hz,
    grid_hz[-1])]``. Report-only (see :class:`LinearizationFit`'s docstring).

    ``target_curve_db`` is per-bin since R10a (#1817): a flat array at
    ``target_level_db`` reproduces the pre-R10a number exactly, and the
    crossover-shaped curve stops this band — which deliberately runs an OCTAVE
    PAST the fit band, straight through the handoff — from scoring a branch's
    own crossover rolloff as residual. That was the largest single
    overstatement in the claim: the rolloff is the graph working, not error.
    """
    verify_hi_hz = min(2.0 * fit_hi_hz, float(grid_hz[-1]))
    verify_band_hz = (fit_lo_hz, verify_hi_hz)
    verify_mask = (grid_hz >= fit_lo_hz) & (grid_hz <= verify_hi_hz)
    residual = (working_db - target_curve_db)[verify_mask]
    rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    max_db = float(np.max(np.abs(residual))) if residual.size else 0.0
    return verify_band_hz, rms_db, max_db


def _observe_octave_summary(
    grid_hz: np.ndarray, working_db: np.ndarray, target_curve_db: np.ndarray,
) -> dict[str, float]:
    """The honesty ladder's OBSERVE level: per-octave achieved-vs-target
    magnitude to the grid's own top (20 kHz on the production grid),
    independent of the fit/verify bands — the disclosure layer (see
    :class:`LinearizationFit`'s docstring). Mirrors
    :func:`_octave_band_reason_summary`'s own octave-center sampling
    (same :data:`_OCTAVE_BAND_CENTERS_HZ`, same "nearest grid bin" pick,
    same range guard), so the two dicts key identically band-for-band.

    ``target_curve_db`` is per-bin since R10a (#1817); a flat array at
    ``target_level_db`` reproduces the pre-R10a numbers exactly. This band
    runs to the WHOLE grid, so on a two-way branch most of its octaves sit in
    a stopband — where a flat target reported the crossover's own attenuation
    as a deficit of tens of dB.
    """
    out: dict[str, float] = {}
    for center in _OCTAVE_BAND_CENTERS_HZ:
        if center < grid_hz[0] or center > grid_hz[-1]:
            continue
        idx = int(np.argmin(np.abs(grid_hz - center)))
        out[str(int(center))] = float(working_db[idx] - target_curve_db[idx])
    return out


def _core_or_fallback_mask(
    envelope: EnvelopeCurve, envelope_mask: np.ndarray,
) -> np.ndarray:
    """The "core passband" — bins where BOTH mic-trust and class-prior still
    sit at the ceiling sentinel (not yet tapering) — intersected with the
    fit-eligible mask. Falls back to the whole fit-eligible mask when the
    core is empty (an aggressively-tapered tier/class with no untapered
    region at all — e.g. a "phone" tier whose mic-trust taper starts low).
    """
    mic_trust = envelope.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    class_prior = envelope.terms[ReasonCode.LIMITED_BY_CLASS_PRIOR]
    core = (
        np.isclose(mic_trust, ENVELOPE_CEILING_SENTINEL_DB)
        & np.isclose(class_prior, ENVELOPE_CEILING_SENTINEL_DB)
        & envelope_mask
    )
    return core if core.any() else envelope_mask


#: Narrowest span, in octaves, a core-level MEDIAN may be read over (#1929).
#:
#: Not a magic number: this fit ladder-smooths at 1/6 octave below 4 kHz
#: (:func:`_ladder_smooth`), so bins closer together than that are not
#: independent samples of anything. A third of an octave is two smoothing
#: kernels — the narrowest span on which "the median" is a statistic rather
#: than one smoothed point wearing a statistic's name. On the production grid
#: (:data:`~jasper.active_speaker.linearization_envelope.
#: DEFAULT_ENVELOPE_GRID_HZ`, 176 log bins over 150 Hz-20 kHz ≈ 24.8 per
#: octave) that is about 8 bins.
#:
#: What it protects against is a DISCONTINUITY, not a wrong answer. A tweeter
#: whose radiating band starts just inside its core mask's top edge gets a
#: one-bin intersection, and one bin narrower it is empty. Measured at
#: reference tier on a flat tweeter declared 300 Hz-20 kHz, the raw
#: intersection alone read −2.847 dB from a single bin at LR2 Fc 3750 and
#: −19.401 dB from the whole-mask fallback at Fc 3775 — 16.55 dB across 25 Hz
#: of crossover frequency, into a gate whose whole tolerance is 3.0 dB.
#:
#: **How the floor is applied is the whole design, and DISCARDING is the wrong
#: way.** A first cut treated a sub-floor intersection as unusable and fell
#: back to the whole core mask. That does not remove the cliff, it MOVES it —
#: to wherever the intersection crosses the floor width, which is lower and
#: more common Fc: measured, LR2 2900→2920 Hz stepped −2.286 → −15.427
#: (13.14 dB over 20 Hz) and LR4 3625→3650 stepped −1.803 → −35.642 (33.84 dB
#: over 25 Hz), i.e. straight into ordinary two-way crossover territory.
#:
#: So a sub-floor intersection is WIDENED to exactly this width instead, never
#: discarded — and the rule is TWO-SIDED, which the first version of it was
#: not. Widening runs downward from the intersection's own top edge; if the
#: core mask's bottom stops that short, the remaining deficit is made up
#: UPWARD from the core's bottom bin.
#:
#: Both directions are needed because the two roles run out of room at
#: opposite ends. A tweeter's intersection is pinned against the core mask's
#: TOP (its high-pass edge slides up into it), so there is always passband
#: below to widen into. A woofer's slides DOWN — its low-pass edge sits at
#: ~0.80*Fc — and meets a room gate that has raised the trusted floor, so
#: below is where the room is gone. Downward-only widening therefore no-ops
#: on the woofer side exactly when it is needed: measured with a 600 Hz gate,
#: Fc 760 read a 1-bin median with the floor silently inactive, and a 400 Hz
#: gate did the same at Fc 520 — 23-30 dB steps in the neighbourhood,
#: reachable whenever Fc <= 1.57x the validity floor (an ordinary
#: horn-in-a-room shape). Widening upward spends at most one floor width of
#: the woofer's own low-pass skirt, which is the same trade the tweeter side
#: already makes into its high-pass knee.
#:
#: The result is continuous by construction: an exact no-op at the boundary
#: width, a constant-width band below it, the shrinking intersection above it
#: — and the estimate stays a statement about THIS driver rather than a
#: whole-mask number tens of dB away. Two cases still take the whole mask, and
#: :func:`core_level_band_hz` discloses both: a genuinely EMPTY intersection,
#: and a core mask that is itself narrower than the floor (nothing left to
#: widen into, and the whole mask is what widening was converging on anyway).
_MIN_LEVEL_BAND_OCTAVES: float = 1.0 / 3.0


def _spans_floor(lo_hz: float, hi_hz: float) -> bool:
    """Is ``[lo_hz, hi_hz]`` at least :data:`_MIN_LEVEL_BAND_OCTAVES` wide?

    One predicate so the "is this enough band" question is asked identically
    of the raw intersection and of each widened candidate — asking it two ways
    is how the first version of this floor came to be silently inactive on the
    woofer side.
    """
    return lo_hz > 0.0 and math.log2(hi_hz / lo_hz) >= _MIN_LEVEL_BAND_OCTAVES


def _core_level_mask(
    envelope: EnvelopeCurve,
    envelope_mask: np.ndarray,
    radiating_band_hz: tuple[float, float] | None,
) -> np.ndarray:
    """The bins a core-level median runs over: the core mask, narrowed to
    ``radiating_band_hz``, widened back to :data:`_MIN_LEVEL_BAND_OCTAVES` if
    that narrowing left less band than a median can be taken over.

    THE one implementation of that rule — :func:`driver_core_level_db` and
    :func:`core_level_band_hz` both bottom out here, so the level and the band
    disclosed beside it are always the same decision.

    Three outcomes, and the middle one is the design (see
    :data:`_MIN_LEVEL_BAND_OCTAVES`):

    * the intersection is at least a floor's width — use it;
    * it is narrower — WIDEN it downward from its own top edge to exactly a
      floor's width, clamped inside the core mask. Continuous by construction,
      and the level stays a statement about the band this driver radiates in;
    * it is EMPTY — fall back to the whole core mask. A three-way mid squeezed
      between two crossovers closer together than their own edges honestly has
      no radiating band at all (:func:`~jasper.active_speaker.branch_chain.
      radiating_band_hz` documents that case), and a driver whose level is
      read over a wider-than-ideal band is still a measured level; dropping it
      from the frame instead would let one squeezed role stop grading every
      other one. This is the one path that changes what the number MEANS, so
      it is the one :func:`core_level_band_hz` exists to disclose.
    """
    core = _core_or_fallback_mask(envelope, envelope_mask)
    if radiating_band_hz is None:
        return core
    lo_hz, hi_hz = radiating_band_hz
    grid_hz = envelope.freqs_hz
    narrowed = core & (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
    if not narrowed.any():
        return core
    used = grid_hz[narrowed]
    lo_used, hi_used = float(used[0]), float(used[-1])
    if _spans_floor(lo_used, hi_used):
        return narrowed

    # Sub-floor. Widening is done on the core mask's OWN bins, and each edge is
    # snapped OUTWARD to the first bin that actually reaches the floor width —
    # snapping inward leaves the result a bin short of the floor it just asked
    # for, which silently sends every widened band to the whole-mask fallback.
    core_idx = np.flatnonzero(core)
    core_freqs = grid_hz[core_idx]
    span = 2.0 ** _MIN_LEVEL_BAND_OCTAVES

    # DOWN from this intersection's own top edge first. Anchoring on
    # ``hi_used`` rather than on the crossover is what makes the neighbourhood
    # continuous — at exactly the floor width the widened band IS the
    # intersection, and below it the band stops moving instead of being swapped
    # for another. This is the tweeter case: the room below is its passband.
    top = int(np.searchsorted(core_freqs, hi_used, side="right")) - 1
    bottom = int(np.searchsorted(core_freqs, hi_used / span, side="right")) - 1
    if bottom < 0:
        # Downward room is exhausted — the WOOFER case, where a low Fc slides
        # ``hi_used`` down to meet a room gate that raised the trusted floor.
        # Make the deficit up UPWARD from the core's own bottom bin instead.
        # That spends at most one floor width of the driver's own low-pass
        # skirt, the same trade the tweeter side already makes into its
        # high-pass knee, and it keeps the estimate a statement about THIS
        # driver rather than a whole-mask number tens of dB away.
        bottom = 0
        top = int(np.searchsorted(core_freqs, core_freqs[0] * span, side="left"))
        if top >= core_freqs.size:
            # Neither direction has room: the core mask is itself narrower than
            # the floor. Nothing left to widen into, so take the documented
            # whole-mask fallback — which for a core this small is what the
            # widening was converging on anyway, so the neighbourhood stays
            # continuous.
            return core

    widened = np.zeros_like(core)
    widened[core_idx[bottom:top + 1]] = True
    return widened


def _target_and_plateau_db(
    smoothed_db: np.ndarray, level_mask: np.ndarray,
) -> tuple[float, float]:
    """``(target_level_db, plateau_level_db)`` — the design doc's "Target
    level" rule (median, NOT the band minimum — proven bad on real data)
    plus the coordinator's normalization-budget plateau (the SAME region's
    own maximum).
    """
    band = smoothed_db[level_mask]
    return float(np.median(band)), float(np.max(band))


def driver_core_level_db(
    primary: DriverResponse, envelope: EnvelopeCurve,
    *, radiating_band_hz: tuple[float, float] | None = None,
) -> float | None:
    """One driver's own passband level — a SUBORDINATE level estimate.

    It runs :func:`fit_driver_linearization`'s own resample → ladder-smooth →
    core-mask → median chain, and exists as a separate entry point because it
    is read across ALL drivers before any one of them is fitted.

    **It does not place the trim pair, and since #2609 nothing derived from it
    does.** The pair is anchored on the RAW MEASURED TRIM
    (:func:`~jasper.active_speaker.crossover_v2.intervention.anchor_trims`).
    This number is one of the two per-driver estimates that
    :func:`~jasper.active_speaker.crossover_v2.intervention.check_level_consistency`
    compares against the other: agreement is reassurance, disagreement banks a
    finding and flags the capture as retriable, and neither outcome changes the
    anchor. The two-voter arbitration this used to feed — and the 3.0 dB cliff
    it hung on — are deleted; see that function for why no estimator wins.

    ``radiating_band_hz`` (#1929) narrows the median's band to where this
    driver's own crossover leaves it radiating —
    :func:`jasper.active_speaker.branch_chain.radiating_band_hz`, solved by
    the composer, so nothing here knows what a crossover is. ``None`` (the
    default, and every caller before #1929) is the pre-#1929 whole-core-mask
    median, byte for byte. A branch with no crossover sections is NOT that
    case: it gets ``(0.0, inf)`` from the solver, which narrows nothing, so a
    caller never has to decide between the two. The narrowing is subject to
    :func:`_core_level_mask`'s width floor — a bound that leaves too little
    band to take a median over is not applied, and
    :func:`core_level_band_hz` reports which way that went.

    **Why the band matters, and why only here.** The core mask is bounded by
    the driver's declared ``measurement_band_hz`` — a CAPTURE-COVERAGE
    declaration, "sweep me over this span", which routinely reaches well past
    the session's Fc. A branch is measured THROUGH its own crossover, so every
    declared bin past the handoff carries that crossover's stopband, and a
    MEDIAN is a rank statistic: a −40 dB stopband bin counts exactly as much
    as a passband one. On the 2026-07-30 JTS3 session (#1870) the woofer was
    declared to 4000 Hz against a 2000 Hz LR4, putting ~28% of its core bins
    an octave inside its own stopband and reading its level 3.4 dB away from
    the trim solve's mirrored ±1-octave estimate of the same physical
    quantity — past :data:`~jasper.active_speaker.crossover_v2.intervention.
    LEVEL_ESTIMATOR_TOLERANCE_DB`, which then refused a healthy speaker. Two
    identical flat drivers behind a matched LR4 pair reproduce it at 9.4 dB.
    (Crossing that tolerance no longer refuses anything — see
    :func:`~jasper.active_speaker.crossover_v2.intervention.check_level_consistency`
    — but the contamination this paragraph describes is still real and the band
    is still the fix for it.)

    The contamination is specific to the rank statistic, so the fix is. The
    give-back (:attr:`LinearizationFit.correction_giveback_db`) is a
    POWER-domain band average over the same mask and is already effectively
    immune — quiet stopband bins contribute almost nothing to a power mean —
    and the fit's own ``target_level_db`` is a different question with a
    different right answer (see :func:`fit_driver_linearization`).

    Returns ``None`` — not a number — when the envelope allows correction
    nowhere. That driver's level is UNKNOWN, and a frame is a claim about
    where drivers sit relative to each other; feeding it a placeholder would
    let one unmeasurable driver move every other one. The caller leaves such a
    role out of the frame, which leaves its offset at 0 and its branch where
    the trim solve already put it.
    """
    grid_hz = envelope.freqs_hz
    smoothed_db = _ladder_smooth(
        grid_hz, np.interp(grid_hz, primary.freqs_hz, primary.magnitude_db)
    )
    envelope_mask = envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB
    if not envelope_mask.any():
        return None
    return _target_and_plateau_db(
        smoothed_db, _core_level_mask(envelope, envelope_mask, radiating_band_hz),
    )[0]


def core_level_band_hz(
    envelope: EnvelopeCurve, *,
    radiating_band_hz: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """The span :func:`driver_core_level_db` ACTUALLY reads its median over,
    for the same arguments — ``None`` when it would return ``None``.

    Exists so a caller that discloses the band (the session's refusal
    journal) discloses the realized one rather than the bound it asked for.
    The divergence that MATTERS is :func:`_core_level_mask`'s width floor
    refusing the bound, which is precisely the case a reader diagnosing a
    refusal needs told rather than hidden. But the two are not otherwise
    equal: this band is resolved onto the envelope's own grid, so its edges
    are the outermost BINS inside the requested span and a snap of up to one
    bin is the ordinary case rather than a signal (measured on the 2026-08-16
    jts3 woofer: 1291.4105 returned against a 1321.3 Hz declared edge — bin
    77, with bin 78 at 1328.0267 already outside it). Read a difference wider
    than a bin as the floor firing; read a sub-bin one as quantization.

    Both functions bottom out in the one helper, so the number logged and the
    number used cannot drift.
    """
    envelope_mask = envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB
    if not envelope_mask.any():
        return None
    used = envelope.freqs_hz[
        _core_level_mask(envelope, envelope_mask, radiating_band_hz)
    ]
    return (float(used[0]), float(used[-1]))


def _adaptive_band_trim(
    grid_hz: np.ndarray,
    smoothed_db: np.ndarray,
    envelope_mask: np.ndarray,
    target_level_db: float,
) -> tuple[int, int]:
    """Adaptive fit-band trim (design doc "Layer 1a concretely" — the
    scoping experiment's mechanism that pulled a real woofer's edge from
    4000 to ~2600 Hz as it rolled off approaching its own crossover point).
    Returns inclusive ``(lo_idx, hi_idx)`` grid indices.

    The seed is CURVE-SHAPE-DRIVEN, not trust-driven: the extremes of
    ``envelope_mask`` bins whose smoothed value is already within one
    cut-budget of ``target_level_db`` (``smoothed_db >= target - cut_budget``
    — the SAME per-filter cut budget the peaking loop's per-bin caps use).
    This is deliberately NOT the mic-trust/class-prior "core" region
    (:func:`_core_or_fallback_mask`, used only for the target/plateau level):
    a driver's own natural acoustic rolloff toward its crossover point has
    nothing to do with mic trust or driver class, and for a woofer band
    entirely below its class/tier taper breakpoints the "core" spans the
    WHOLE envelope-eligible range — seeding from ITS extremes would start
    the walk already at the outer edge, with no room left to trim the
    rolloff at all (the bug an earlier version of this function had).

    From that seed, extends outward toward each edge of ``envelope_mask``,
    stopping the FIRST time either: the smoothed curve drops below the
    floor, or ``envelope_mask`` itself ends (handles a non-contiguous mask
    safely, though a contiguous mask is the overwhelmingly common case —
    the OUT_OF_BAND premask plus smooth monotone tapers make one).
    """
    idxs = np.flatnonzero(envelope_mask)
    floor_db = target_level_db - PER_FILTER_CUT_CAP_DB
    within_budget = envelope_mask & (smoothed_db >= floor_db)
    seed_idxs = np.flatnonzero(within_budget)
    if seed_idxs.size:
        seed_lo, seed_hi = int(seed_idxs[0]), int(seed_idxs[-1])
    else:
        # Degenerate: no bin anywhere is within budget of target (a wildly
        # noisy or ill-fitting target). Seed from the single closest bin so
        # the walk below still has somewhere to start; both loops then find
        # that bin itself already violates (or exactly meets) the floor and
        # go no further, collapsing to a 1-bin band rather than crashing.
        nearest = int(idxs[np.argmin(np.abs(smoothed_db[idxs] - target_level_db))])
        seed_lo = seed_hi = nearest

    lo_bound = int(idxs[0])
    fit_lo_idx = seed_lo
    for i in range(seed_lo, lo_bound - 1, -1):
        if not envelope_mask[i] or smoothed_db[i] < floor_db:
            break
        fit_lo_idx = i

    hi_bound = int(idxs[-1])
    fit_hi_idx = seed_hi
    for i in range(seed_hi, hi_bound + 1):
        if not envelope_mask[i] or smoothed_db[i] < floor_db:
            break
        fit_hi_idx = i

    return fit_lo_idx, fit_hi_idx


def _solve_band_mask(
    grid_hz: np.ndarray,
    band_mask: np.ndarray,
    radiating_band_hz: tuple[float, float] | None,
) -> np.ndarray:
    """``band_mask`` narrowed to the bins this branch still materially reaches
    the summed response in (#2523) — the declared radiating band widened by
    :data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`.

    This is the band the solve's OBJECTIVE runs over. Bins outside it are
    EXCLUDED, not given a zeroed target: a zeroed target is still a demand, and
    ``design_peq`` is greedy over the residue, so a bin that reads "you are
    52.9 dB too loud" wins the search whatever the target says. Excluding it
    means the search never sees it.

    **Why the band has to be widened, and why by THIS margin.** The declared
    radiating band is the -3 dB span, and masking the solve at exactly that
    edge refuses cuts this repo has twice measured as genuine work: the
    resonance 0.17 octaves past the edge in
    ``test_the_bound_is_boost_only_cuts_still_reach_out_of_band_leakage``, and
    the conductor fixture's tweeter bump 0.39 octaves inside its high-pass edge
    that "turned a passing correction into a refused one". Both are bins where
    the crossover is only a few dB down and the branch is still most of the
    sum. The margin is the SAME half octave
    :attr:`~jasper.active_speaker.branch_target.BranchTarget.gain_band_hz`
    already widens the same band by, imported rather than restated, and the
    reason it transfers is that its derivation transfers: half an octave past
    an LR4 edge the branch is 8.46 dB down and still moves the sum by 0.285 dB
    per dB of its own change (~29% leverage — see that constant's own comment).
    That leverage argument is direction-agnostic; a cut moves the sum by the
    same 0.285 dB per dB a boost does. One physical question, one number. Two
    constants here would be the drift, not the safety.

    ``None`` — every caller before #1809, and a one-way box's summed chain —
    narrows nothing. An intersection that comes back EMPTY also narrows
    nothing: a mid squeezed between two crossovers closer together than their
    own edges honestly has no radiating band (:func:`~jasper.active_speaker.
    branch_chain.radiating_band_hz` documents that case), and refusing it every
    bin would be a worse answer than the pre-#2523 one. Same fallback posture,
    and the same reason, as :func:`_core_level_mask`'s empty intersection.
    """
    if radiating_band_hz is None:
        return band_mask
    lo_hz, hi_hz = radiating_band_hz
    narrowed = band_mask & (
        (grid_hz >= octave_scaled(lo_hz, -STOPBAND_GAIN_MARGIN_OCTAVES))
        & (grid_hz <= octave_scaled(hi_hz, STOPBAND_GAIN_MARGIN_OCTAVES))
    )
    return narrowed if narrowed.any() else band_mask


# A falling top octave IS now compensated — by ``_hf_continuation_stage``
# below, NOT by the falling-slope Lowshelf once sketched here. That earlier
# "corner the shelf at ``fit_lo_hz``" guidance was the wrong shape: a shelf
# cornered at the fit band's LOW edge would pull the whole band down, and its
# budget accounting would have had to cover spend below ``target_level_db``.
# The ruled CD-horn design (owner ruling + 4-lens panel, 2026-07-24) instead
# corners a Lowshelf near the deficit's ONSET and works in the cut domain: it
# cuts everything below the compensation region by ``spend`` so the flow's
# trim re-solve levels the branches back and the top octave lands ``spend`` dB
# higher RELATIVELY (the measured-inverse give-back). See
# ``_hf_continuation_stage`` and docs/active-speaker-tuning-layers-design.md
# "Layer 1a" HF section. ``_shelf_stage`` below stays the RISING-slope
# Highshelf; the two are mutually exclusive (a genuinely rising response has
# no falling top-octave deficit to compensate).
def _shelf_stage(
    grid_hz: np.ndarray,
    smoothed_db: np.ndarray,
    band_mask: np.ndarray,
    fit_lo_hz: float,
    fit_hi_hz: float,
    target_level_db: float,
    plateau_level_db: float,
    *,
    shape_db: np.ndarray | None = None,
) -> LinearizationFilter | None:
    """Fit ONE cut-only Highshelf if the fit band's smoothed slope rises
    faster than :data:`SHELF_SLOPE_THRESHOLD_DB_PER_OCT`. Returns ``None``
    when no shelf is warranted (falling/shallow slope, too few points to
    regress, or the normalization budget leaves nothing to spend).

    Dormant for falling-slope drivers by design — a cut-only shelf cannot
    correct a naturally falling response; the deferred Lowshelf counterpart
    for that case is documented in the comment block above this function.

    ``shape_db`` (R10a, #1817) is the branch's re-centred crossover shape, and
    the regression runs on ``smoothed_db - shape_db`` — the branch's OWN
    slope, with its crossover's contribution removed.

    **What is measured, and what is not.** On a FLAT tweeter behind a 2 kHz
    LR4 high-pass, regressed over [800, 18000] Hz, the raw band slope is
    **+5.6957 dB/oct** and the shape-removed slope is **0.0000** — so the
    slope gate (threshold 3.0) is armed by the crossover alone, on a driver
    with nothing wrong with it. That much is demonstrated
    (``test_the_shelf_slope_gate_reads_the_crossover_not_the_driver``).

    What is NOT claimed is that this reaches an emitted shelf. It does not, in
    that case: with the band's low edge deep in the high-pass rolloff the
    corner selection takes the LOW side, whose drop below target is negative,
    and the stage returns ``None`` anyway. The gate is being asked the wrong
    question and is saved by an unrelated downstream test — which is a reason
    to fix the question, not to rely on the rescue. ``None`` regresses the raw
    curve, exactly as before R10a.
    """
    if int(band_mask.sum()) < 2:
        return None
    slope_curve_db = smoothed_db if shape_db is None else smoothed_db - shape_db
    log2_f = np.log2(grid_hz[band_mask])
    slope_db_per_oct, intercept = np.polyfit(log2_f, slope_curve_db[band_mask], 1)
    if slope_db_per_oct <= SHELF_SLOPE_THRESHOLD_DB_PER_OCT:
        return None

    pred_lo = slope_db_per_oct * math.log2(fit_lo_hz) + intercept
    pred_hi = slope_db_per_oct * math.log2(fit_hi_hz) + intercept
    dev_lo = abs(pred_lo - target_level_db)
    dev_hi = abs(pred_hi - target_level_db)
    if dev_hi >= dev_lo:
        corner_hz, total_drop_db = fit_hi_hz, max(0.0, pred_hi - target_level_db)
    else:
        corner_hz, total_drop_db = fit_lo_hz, max(0.0, pred_lo - target_level_db)
    if total_drop_db <= 0.0:
        return None

    # Coordinator's normalization-budget clamp: how much of the total spend
    # budget is left once the plain target-vs-plateau gap is accounted for.
    # See MAX_NORMALIZATION_SPEND_DB's docstring for the full reasoning.
    remaining_budget_db = max(
        0.0, MAX_NORMALIZATION_SPEND_DB - (plateau_level_db - target_level_db)
    )
    shelf_cut_db = min(total_drop_db, PER_FILTER_CUT_CAP_DB, remaining_budget_db)
    if shelf_cut_db < _MIN_FILTER_GAIN_DB:
        return None
    return LinearizationFilter(
        biquad_type="Highshelf", freq=corner_hz, q=_HIGHSHELF_Q, gain=-shelf_cut_db,
    )


@dataclass(frozen=True)
class _HfContinuation:
    """Result of :func:`_hf_continuation_stage`.

    ``filters`` is empty on every non-firing path (ineligible, skipped, or
    suppressed). When the stage FIRES it is ``(lowshelf, *peaking_cuts,
    taper?)`` in the emitter's shelf-first / taper-last order — the caller
    inserts ``filters[0]`` (the Lowshelf backbone) at position 0 of the fit's
    filter list and appends the rest. Every other field is zeroed/empty unless
    the stage fired; ``suppressed_reason`` is the sole non-empty field on an
    objective-gate suppression, everything else zeroed there too.
    """

    filters: tuple[LinearizationFilter, ...]
    spend_db: float
    ceiling_hz: float
    policy: str
    suppressed_reason: str
    measured_deficit_at_ceiling_db: float


# The empty/zeroed "did not fire, no objective suppression" result — an
# ineligible driver or a nothing-to-do skip. Shared so those paths cannot
# drift from each other.
_HF_INERT = _HfContinuation(
    filters=(), spend_db=0.0, ceiling_hz=0.0, policy="",
    suppressed_reason="", measured_deficit_at_ceiling_db=0.0,
)


def _hf_suppressed(reason: str) -> _HfContinuation:
    """An objective-gate suppression: no filters, only ``suppressed_reason``
    set, everything else zeroed (design ruling: suppression is visible and
    named, never a silent no-op)."""
    return _HfContinuation(
        filters=(), spend_db=0.0, ceiling_hz=0.0, policy="",
        suppressed_reason=reason, measured_deficit_at_ceiling_db=0.0,
    )


def _hf_confidence_ceiling_and_knee_hz(
    grid_hz: np.ndarray, mic_trust_term: np.ndarray,
) -> tuple[float, float]:
    """``(ceiling_hz, knee_hz)`` from the mic-trust term's own taper.

    ``ceiling_hz`` is the first bin where mic-trust reaches ~0 (its taper-zero
    — the frequency above which the calibrated mic resolves nothing, ~16.4 kHz
    on the reference tier); ``grid_hz[-1]`` if the term never reaches 0 on this
    grid. ``knee_hz`` is the first bin BELOW the ceiling sentinel (where the
    taper begins) — the ``np.isclose(term, ENVELOPE_CEILING_SENTINEL_DB)``
    test is deliberately the same one :func:`_core_or_fallback_mask` uses so
    "still fully trusted" means one identical thing across this module.
    """
    zero_bins = np.flatnonzero(np.isclose(mic_trust_term, 0.0))
    ceiling_hz = (
        float(grid_hz[int(zero_bins[0])]) if zero_bins.size else float(grid_hz[-1])
    )
    below_sentinel = np.flatnonzero(
        ~np.isclose(mic_trust_term, ENVELOPE_CEILING_SENTINEL_DB)
    )
    knee_hz = (
        float(grid_hz[int(below_sentinel[0])])
        if below_sentinel.size else float(grid_hz[-1])
    )
    return ceiling_hz, knee_hz


def _hf_repeat_spread_ok(
    grid_hz: np.ndarray,
    primary: DriverResponse,
    ceiling_hz: float,
) -> str:
    """The repeat-agreement gate. Returns an empty string when the repeats
    agree well enough over the compensation band, else a suppression reason
    (``"insufficient_repeats"`` / ``"repeat_disagreement"``).

    Spread is the per-bin ``max - min`` across ALL of the capture's sweep
    occurrences — the PRIMARY plus its ``repeat_responses`` — matching
    :func:`jasper.active_speaker.linearization_envelope.compute_sigma_curve`'s
    own occurrence set. The primary MUST be in the spread: the fit is sized
    from the primary, so a primary that carries an outlier artifact its repeats
    do not reproduce (e.g. a −12 dB top-octave glitch while two repeats agree
    at −5) has to be caught here, or the stage would size a several-dB-too-hot
    lift from that one bad sweep. Each occurrence is resampled + ladder-smoothed
    onto the grid exactly as the primary is. Fewer than
    :data:`_HF_MIN_OCCURRENCES` total occurrences is no reproducibility
    evidence (the N>=3-total "paired gate," sigma-seeding report finding 5,
    consistent with ``crossover_v2.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES``)
    → suppressed. Otherwise the spread must stay under
    :data:`HF_AGREEMENT_LIMIT_LOW_DB` below
    :data:`_HF_AGREEMENT_TIER_SPLIT_HZ` and
    :data:`HF_AGREEMENT_LIMIT_HIGH_DB` from there to the ceiling.
    """
    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    if len(occurrences) < _HF_MIN_OCCURRENCES:
        return "insufficient_repeats"
    smoothed = np.stack([
        _ladder_smooth(grid_hz, np.interp(grid_hz, o.freqs_hz, o.magnitude_db))
        for o in occurrences
    ])
    spread = np.max(smoothed, axis=0) - np.min(smoothed, axis=0)
    band = (grid_hz >= HF_COMPENSATION_BAND_LO_HZ) & (grid_hz <= ceiling_hz)
    low = band & (grid_hz < _HF_AGREEMENT_TIER_SPLIT_HZ)
    high = band & (grid_hz >= _HF_AGREEMENT_TIER_SPLIT_HZ)
    if np.any(spread[low] > HF_AGREEMENT_LIMIT_LOW_DB) or np.any(
        spread[high] > HF_AGREEMENT_LIMIT_HIGH_DB
    ):
        return "repeat_disagreement"
    return ""


def _hf_continuation_stage(
    grid_hz: np.ndarray,
    working_db: np.ndarray,
    target_curve_db: np.ndarray,
    target_level_db: float,
    plateau_level_db: float,
    envelope: EnvelopeCurve,
    primary: DriverResponse,
    fit_lo_hz: float,
    fit_hi_hz: float,
    filters: Sequence[LinearizationFilter],
) -> _HfContinuation:
    """The CD-horn compensation stage (#1668): measured-inverse top-octave
    lift, realized cut-only via give-back. Runs AFTER the peaking loop.

    The tweeter-on-a-horn measures a real, EQ-able falling top octave (the
    horn's constant-directivity rolloff, not driver mass). This stage sizes a
    lift from that MEASURED deficit (class-blind), realizes it in the CUT
    domain — cut everything below the compensation region by ``spend`` so the
    flow's trim re-solve levels the branches back and the top octave lands
    ``spend`` dB higher RELATIVELY — and, only above the confidence ceiling
    where nothing is measurable, applies a declared-driver-class continuation
    policy (hold / taper). Objective gates (fit-band reach, repeat agreement,
    realization fit-quality) suppress it rather than ship a guess. See the
    module's own falling-slope note above ``_shelf_stage`` and
    docs/active-speaker-tuning-layers-design.md.

    Every filter it can emit is a cut (Lowshelf backbone + Peaking cuts +
    optional Highshelf taper, all gain <= 0) — the fit engine's cut-only
    invariant binds here too.
    """
    mic_trust = envelope.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    ceiling_hz, knee_hz = _hf_confidence_ceiling_and_knee_hz(grid_hz, mic_trust)

    # -- Applicability (not-applicable → silent inert result) --------------
    # Fit band must reach the confidence-ceiling region: a woofer/mid whose
    # trimmed fit band tops out below the mic knee has no top-octave deficit
    # to compensate. This is what keeps the stage role-agnostic without a
    # per-role branch. A rising-slope Highshelf already emitted means the stage
    # does not apply at all (mutual exclusivity: a genuinely rising response
    # has no falling top-octave deficit). Both are "not this driver" — inert,
    # no reason.
    if fit_hi_hz < knee_hz:
        return _HF_INERT
    if any(f.biquad_type == "Highshelf" for f in filters):
        return _HF_INERT
    # The stage APPLIES but the flattening loop already spent every slot — a
    # named suppression (observable via /state + doctor), not a silent inert,
    # because it means an eligible CD-horn lift was dropped for lack of budget.
    if len(filters) >= MAX_FILTERS_PER_DRIVER:
        return _hf_suppressed("no_filter_budget")

    # -- Agreement gate (objective suppression) ----------------------------
    disagreement = _hf_repeat_spread_ok(grid_hz, primary, ceiling_hz)
    if disagreement:
        return _hf_suppressed(disagreement)

    ceiling_idx = int(np.argmin(np.abs(grid_hz - ceiling_hz)))

    # -- Desired compensation C(f) (the measured inverse) ------------------
    #
    # Against the target CURVE since R10a (#1817). On the branch this stage
    # actually fires for — a horn tweeter, whose compensation region is its
    # TOP octave, far above its own high-pass — the crossover shape is ~0 dB
    # there, so this is near-identical to the old scalar in practice. It reads
    # the curve anyway because the stage's ONSET walk runs down toward
    # ``trusted_mid_hz`` looking for where the deficit first turns positive,
    # and a scalar target would let a branch's own crossover rolloff open that
    # walk early and size the compensation off it.
    deficit_db = target_curve_db - working_db
    measured_deficit_at_ceiling_db = float(max(0.0, deficit_db[ceiling_idx]))

    # Onset: the first bin ABOVE the trusted band's lower half (its geometric
    # midpoint) where the smoothed deficit rises through _MIN_FILTER_GAIN_DB
    # AND stays positive all the way to the ceiling (a contiguous, real
    # falling region — not a lone blip in the otherwise-flat trusted band).
    # The trusted band is [fit_lo_hz, knee_hz]; its lower-half boundary is the
    # geometric midpoint (log-domain, consistent with the smoothing ladder).
    trusted_mid_hz = math.sqrt(max(fit_lo_hz, 1.0) * knee_hz)
    onset_idx: int | None = None
    for i in range(len(grid_hz)):
        if grid_hz[i] <= trusted_mid_hz or i > ceiling_idx:
            continue
        if deficit_db[i] > _MIN_FILTER_GAIN_DB and bool(
            np.all(deficit_db[i:ceiling_idx + 1] > 0.0)
        ):
            onset_idx = i
            break
    if onset_idx is None or measured_deficit_at_ceiling_db <= 0.0:
        # No contiguous falling top octave (flat or rising) — nothing to do.
        return _HF_INERT
    onset_hz = float(grid_hz[onset_idx])

    remaining_budget_db = max(
        0.0, MAX_NORMALIZATION_SPEND_DB - (plateau_level_db - target_level_db)
    )
    # Three independent ceilings: the measured deficit (never correct more than
    # was measured), the remaining ledger budget, and what a SINGLE Lowshelf can
    # actually realize on a real curve (see HF_SINGLE_SHELF_SPEND_CAP_DB — the
    # binding one in practice today).
    spend = min(
        measured_deficit_at_ceiling_db,
        remaining_budget_db,
        HF_SINGLE_SHELF_SPEND_CAP_DB,
    )
    if spend < _MIN_FILTER_GAIN_DB:
        # Nothing meaningful to give back (the budget is exhausted or the
        # measured deficit is sub-threshold). Skip, no reason — this is an
        # honest no-op, not an objective suppression.
        return _HF_INERT

    # C(f): 0 below onset, the deficit rescaled so it hits exactly ``spend`` at
    # the ceiling, clamped to [0, spend], and held at ``spend`` above the
    # ceiling (the plateau — correction never RISES past confidence).
    scale = spend / measured_deficit_at_ceiling_db
    compensation_db = np.zeros_like(grid_hz)
    band = (np.arange(len(grid_hz)) >= onset_idx) & (np.arange(len(grid_hz)) <= ceiling_idx)
    compensation_db[band] = np.clip(
        np.maximum(0.0, deficit_db[band]) * scale, 0.0, spend
    )
    compensation_db[np.arange(len(grid_hz)) > ceiling_idx] = spend
    # Cut-domain transform: cut_target <= 0 everywhere — -spend below the
    # onset, rising smoothly to 0 at the ceiling, 0 above (the "hold").
    cut_target_db = compensation_db - spend

    # -- Cut-domain realization: Lowshelf backbone + peaking residual ------
    # One Lowshelf cornered near the onset carries the backbone give-back;
    # biquad cascades commute acoustically, so inserting it at filter position
    # 0 (the emitter's shelf-before-peaks contract) is order-safe regardless
    # of the peaking cuts the flattening loop already placed.
    #
    # The shelf's own gain is CLAMPED at PER_FILTER_CUT_CAP_DB — a hard
    # invariant on every emitted filter, independent of the (now larger) total
    # spend budget. When `spend` exceeds the per-filter cap the shelf carries
    # the first 12 dB and the peaking residual fit below absorbs the remainder
    # (`cut_target` below the onset is the full −spend, so the residual
    # design_peq sees is exactly the uncovered depth). Without this clamp a
    # spend of 14+ would emit a single −14 dB biquad and silently break the
    # per-filter ceiling the envelope/emitter both re-validate against.
    shelf_gain_db = -min(spend, PER_FILTER_CUT_CAP_DB)
    lowshelf = LinearizationFilter(
        biquad_type="Lowshelf", freq=onset_hz, q=_HIGHSHELF_Q, gain=shelf_gain_db,
    )
    lowshelf_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response((lowshelf,), grid_hz)), 1e-12)
    )

    # Reserve slots: 1 for the lowshelf, 1 for a taper if the policy wants one.
    policy = HF_CONTINUATION_POLICY[envelope.driver_class]
    slots_free = MAX_FILTERS_PER_DRIVER - len(filters)
    peaking_slots = slots_free - 1
    if policy == "taper" and peaking_slots >= 1:
        peaking_slots -= 1
    peaking_slots = max(0, peaking_slots)

    # Fit the residual (cut_target - lowshelf) with peaking cuts, capped
    # per-bin by min(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db) exactly
    # as the flattening loop does. The cuts land in the TRUSTED band (where the
    # lowshelf floor hasn't fully settled and near its corner), so the honesty
    # envelope's per-bin caps are naturally satisfied; the top octave itself
    # gets NO peaking filter — its lift arrives via the give-back.
    per_bin_cap_db = -np.minimum(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)
    hf_peaks: list[LinearizationFilter] = []
    if peaking_slots > 0:
        peqs = design_peq(
            lowshelf_db, cut_target_db, grid_hz,
            f_low=fit_lo_hz, f_high=ceiling_hz,
            max_filters=peaking_slots,
            max_cut_db=per_bin_cap_db,
            max_boost_db=0.0,
            cuts_only=True,
            flatness_target_db=_HF_RESIDUAL_FLATNESS_TARGET_DB,
            q_max=_PEAKING_Q_MAX,
            min_filter_gain_db=_MIN_FILTER_GAIN_DB,
        )
        hf_peaks = [
            LinearizationFilter(biquad_type="Peaking", freq=p.freq, q=p.q, gain=p.gain)
            for p in peqs
        ]

    # Fit-quality check: the realized cut cascade (lowshelf + peaks) must track
    # cut_target across [onset, ceiling] to within HF_REALIZATION_TOLERANCE_DB,
    # or the whole stage is suppressed rather than ship a mis-shaped lift. The
    # single complex_correction_response seam (never a magnitude duplicate).
    realized = tuple([lowshelf, *hf_peaks])
    realized_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(realized, grid_hz)), 1e-12)
    )
    check_band = (grid_hz >= onset_hz) & (grid_hz <= ceiling_hz)
    if check_band.any():
        max_err = float(np.max(np.abs(realized_db - cut_target_db)[check_band]))
        if max_err > HF_REALIZATION_TOLERANCE_DB:
            return _hf_suppressed("fit_quality")

    # -- Continuation policy above the ceiling (declared class's authority) -
    emitted = [lowshelf, *hf_peaks]
    if policy == "taper" and len(filters) + len(emitted) < MAX_FILTERS_PER_DRIVER:
        # One trailing Highshelf CUT above ceiling*1.25 walks the relative lift
        # back DOWN across the band we cannot see — protecting an unknown /
        # breakup-prone top from a projected lift with no measurement behind
        # it. Appended LAST (the emitter's taper-last construction contract).
        taper_gain = -min(spend / 2.0, HF_TAPER_MAX_DB)
        if -taper_gain >= _MIN_FILTER_GAIN_DB:
            emitted.append(LinearizationFilter(
                biquad_type="Highshelf", freq=ceiling_hz * 1.25,
                q=_HIGHSHELF_Q, gain=taper_gain,
            ))

    return _HfContinuation(
        filters=tuple(emitted),
        spend_db=spend,
        ceiling_hz=ceiling_hz,
        policy=policy,
        suppressed_reason="",
        measured_deficit_at_ceiling_db=measured_deficit_at_ceiling_db,
    )


# --------------------------------------------------------------------------- #
# the lift stage: reduce our own cuts, then boost (PR-L5)
# --------------------------------------------------------------------------- #

# Bisection iterations used to find how far one existing cut can be shrunk
# without overshooting the desired lift. 24 halvings take a 12 dB search range
# to well under a micro-dB — far finer than any quantity here is meaningful to,
# and the loop runs at most MAX_FILTERS_PER_DRIVER times per fit.
_CUT_REDUCTION_BISECTION_STEPS: int = 24

# Materiality slack (dB) on the permitted-headroom test.
#
# NOT float noise, and 1e-6 was the wrong value (adversarial review N2). A
# biquad's response never reaches exactly 0 dB: a peaking filter cut at 1 kHz
# still leaks a few thousandths of a dB across the whole grid, so at 1e-6 a
# SINGLE far-field bin whose permitted headroom is fractionally negative vetoes
# the entire shrink — measured, a -0.0034 dB leakage bin delivered 0.00 of a
# wanted 4.00 dB, and 0.62 of 3.50 on the real CD-horn fit. The operation was
# technically correct and practically inert.
#
# :data:`_ENVELOPE_NONZERO_EPS_DB` is the module's existing answer to exactly
# this question — "below this, a per-bin dB figure is float noise or a taper's
# asymptotic tail rather than a real allowance" — so it is reused rather than
# given a second name. At 0.05 dB it sits ~15x above the 0.0034 dB leakage that
# caused the veto and 10x below the smallest gain this module will emit
# (:data:`_MIN_FILTER_GAIN_DB` = 0.5), so it can mask neither a real overshoot
# nor a real filter.
_CUT_REDUCTION_EPS_DB: float = _ENVELOPE_NONZERO_EPS_DB


def reduce_cuts_for_lift(
    filters: Sequence[LinearizationFilter],
    wanted_db: np.ndarray,
    headroom_db: np.ndarray,
    grid_hz: np.ndarray,
) -> tuple[tuple[LinearizationFilter, ...], np.ndarray]:
    """Spend a desired lift by SHRINKING cuts we ourselves placed, before any
    boost is considered. Returns ``(filters, delivered_lift_db)``.

    **A first-class operation, distinct from boost** (plan PR-L5). When the fit
    wants more level in a band where one of its own filters is cutting, the
    right move is to cut less there — not to stack an opposing boost on top of
    a cut. Two filters fighting each other cost a slot each, cost headroom the
    shrink would not have cost, and leave a phase response neither of them
    designed. Reducing the cut is free, uses no slot, and is exactly invertible.

    Two arrays, two jobs, deliberately not one:

    * ``wanted_db`` — how much lift would be USEFUL at each bin (``>= 0``).
      Drives which filters are worth touching and when to stop.
    * ``headroom_db`` — how much lift is PERMITTED at each bin. May be
      negative (this bin is already at or above where it should be, so any
      lift here is a regression) and may be ``+inf`` (nothing is claimed
      here, e.g. outside the fit band). This is the SAFETY constraint, and it
      is what stops a cut placed to tame a peak from being unwound to fill an
      unrelated dip an octave away — the peak would simply come back.

    Greedy, deepest cut first: each filter is shrunk by the largest amount
    (bisection) that keeps its delivered lift inside ``headroom_db`` at EVERY
    bin; what it delivered is then subtracted from both arrays and the next
    filter is offered the residue. The delivered lift is computed with the real
    RBJ evaluator at both gains — never a linear-in-gain approximation, which
    is wrong by several tenths of a dB on a shelf and wrong in the unsafe
    direction.

    Filters at or above zero gain are returned untouched: there is no cut to
    reduce. A filter shrunk to within :data:`_MIN_FILTER_GAIN_DB` of unity is
    dropped rather than emitted as a cosmetic residue.
    """
    grid = np.asarray(grid_hz, dtype=np.float64)
    wanted = np.maximum(np.asarray(wanted_db, dtype=np.float64), 0.0)
    permitted = np.asarray(headroom_db, dtype=np.float64)
    delivered_total = np.zeros_like(wanted)
    if not filters or not np.any(wanted > 0.0):
        return tuple(filters), delivered_total

    def _response_db(spec: LinearizationFilter) -> np.ndarray:
        return 20.0 * np.log10(
            np.maximum(np.abs(complex_correction_response((spec,), grid)), 1e-12)
        )

    order = sorted(range(len(filters)), key=lambda i: filters[i].gain)
    out = list(filters)
    for i in order:
        original = out[i]
        if original.gain >= -_MIN_FILTER_GAIN_DB:
            continue
        if not np.any(wanted > 0.0):
            break
        base_db = _response_db(original)
        budget = -float(original.gain)

        def _delivered(shrink_db: float, _base=base_db, _f=original) -> np.ndarray:
            trial = LinearizationFilter(
                biquad_type=_f.biquad_type, freq=_f.freq, q=_f.q,
                gain=_f.gain + shrink_db,
            )
            return _response_db(trial) - _base

        def _fits(shrink_db: float) -> bool:
            return bool(
                np.all(
                    _delivered(shrink_db) <= permitted + _CUT_REDUCTION_EPS_DB
                )
            )

        if _fits(budget):
            shrink = budget
        else:
            lo, hi = 0.0, budget
            for _ in range(_CUT_REDUCTION_BISECTION_STEPS):
                mid = 0.5 * (lo + hi)
                if _fits(mid):
                    lo = mid
                else:
                    hi = mid
            shrink = lo
        if shrink < _MIN_FILTER_GAIN_DB:
            continue
        gained = np.maximum(_delivered(shrink), 0.0)
        delivered_total = delivered_total + gained
        wanted = np.maximum(wanted - gained, 0.0)
        permitted = permitted - gained
        new_gain = original.gain + shrink
        if new_gain >= -_MIN_FILTER_GAIN_DB:
            out[i] = LinearizationFilter(
                biquad_type=original.biquad_type, freq=original.freq,
                q=original.q, gain=0.0,
            )
        else:
            out[i] = LinearizationFilter(
                biquad_type=original.biquad_type, freq=original.freq,
                q=original.q, gain=new_gain,
            )
    kept = tuple(f for f in out if abs(f.gain) >= _MIN_FILTER_GAIN_DB)
    return kept, delivered_total


@dataclass(frozen=True)
class _Lift:
    """Result of :func:`_lift_stage` — the filter list it produced plus its
    own disclosure. ``filters`` is the WHOLE post-stage cascade (the stage may
    have shrunk existing cuts, so it cannot return only its additions)."""

    filters: tuple[LinearizationFilter, ...]
    requested_db: float
    from_reduced_cuts_db: float
    from_boost_db: float
    suppressed_reason: str
    boost_excluded_drops: tuple["BoostExclusionDrop", ...] = ()
    boost_excluded_residual: tuple["BoostExclusionResidual", ...] = ()
    boost_evidence_drops: tuple["BoostEvidenceDrop", ...] = ()


def _boost_evidence_verdicts(
    boosts: Sequence[LinearizationFilter],
    grid_hz: np.ndarray,
    measured_headroom_db: np.ndarray,
    claim_mask: np.ndarray,
) -> tuple[list[LinearizationFilter], list["BoostEvidenceDrop"]]:
    """Split ``boosts`` into the ones the MEASUREMENT supports and the ones it
    contradicts (#2599). Returns ``(kept, dropped)``.

    ``measured_headroom_db`` is ``target − measured``: how far this branch's
    own smoothed, MEASURED response sits BELOW the fit's target at each bin.
    Positive is a measured deficit — level the branch is genuinely missing.
    Zero or negative is a measured excess — the branch is already at or above
    where it should be, and adding level there makes the response worse, not
    better.

    **The criterion is the filter's own half-gain bandwidth**, the same
    intrinsic, per-filter, scale-free action-region test
    :func:`_boost_exclusion_verdicts` already uses and for the same reasons —
    one filter read once against its own transfer function, no ordering, no
    search over subsets, no absolute dB threshold borrowed from a different
    geometry. A boost is dropped when ``measured_headroom_db <= 0`` at EVERY
    bin of its action region: the measurement says there is no deficit
    anywhere this filter would add level. A single bin of genuine measured
    deficit inside the action region keeps it, which is deliberately the most
    permissive form of the rule — this bound exists to catch a boost aimed
    entirely at a measured excess, not to second-guess a boost that has
    something real to fill.

    **Why MEASURED and not the working curve, which is the whole point.** The
    lift stage's own ``deficit_db`` is ``target − working``, and ``working``
    is the measurement plus every cut the stages above already placed. A cut's
    SKIRTS drag the working curve down in a neighbourhood the cut was never
    aimed at, so a cut placed on a real peak can MANUFACTURE a deficit an
    octave away that the measurement does not have — and the lift stage will
    then design a boost to fill it. Two filters fighting each other, both
    charging headroom, at a frequency the driver never needed either of them
    at. Grading against ``working`` cannot see this: by construction the
    working curve agrees that the deficit is real. Grading against the
    measurement can, and it is the same evidence
    :func:`reduce_cuts_for_lift`'s ``headroom_db`` argument is derived from —
    which is exactly the array the boost path never received.

    **What it adds to a gate that grades against a not-a-limit.** The
    realization gate below grades the emitted cascade against
    ``envelope.allowed_depth_db``, which across the whole core passband is
    :data:`~jasper.active_speaker.linearization_envelope.
    ENVELOPE_CEILING_SENTINEL_DB` — the 24.0 dB CEILING SENTINEL, i.e. the
    marker for "this bin expresses no limit". So in the core band its test
    reads ``realized > 24.5``: a threshold carrying no information about what
    the measurement there supports.

    Two honest qualifications, because the stronger claim is tempting and
    wrong. It is not *unreachable* — eight stacked bells at the 12.0 dB
    per-filter cap could clear 24.5 in principle — so this is a statement
    about what the threshold MEANS, not a proof that it never fires. And it
    is not the same question either way: ``allowed_depth_db`` is a
    direction-agnostic correction-DEPTH ceiling, so even when it does fire it
    answers "is this correction deeper than the measurement supports", never
    "is there anything here to lift". For the ~2 dB boost this bound exists
    for it is simply inert.

    That gate is kept — it still binds wherever the envelope actually tapers,
    which is what it was written for. This adds the per-bin, directional
    evidence it never had.
    """
    kept: list[LinearizationFilter] = []
    dropped: list[BoostEvidenceDrop] = []
    for boost in boosts:
        own_db = 20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response((boost,), grid_hz)), 1e-12,
        ))
        action = (own_db >= boost.gain / 2.0) & claim_mask
        if not np.any(action):
            # Nothing the fit makes a claim over — no evidence either way, so
            # this bound abstains rather than refusing. The stopband guard
            # above is what owns a cascade acting entirely outside the band.
            kept.append(boost)
            continue
        headroom = measured_headroom_db[action]
        if float(np.max(headroom)) > 0.0:
            kept.append(boost)
            continue
        acted = grid_hz[action]
        dropped.append(BoostEvidenceDrop(
            freq_hz=float(boost.freq), q=float(boost.q),
            gain_db=float(boost.gain),
            action_band_hz=(float(acted[0]), float(acted[-1])),
            # The LEAST-hot bin it acts on, stated as an excess (positive =
            # the measurement is above target). Reporting the least-hot bin
            # rather than the worst is the honest bound: it is the closest
            # this filter came to having something to fill, and it still had
            # nothing.
            measured_excess_db=float(-np.max(headroom)),
        ))
    return kept, dropped


def _boost_exclusion_verdicts(
    boosts: Sequence[LinearizationFilter],
    grid_hz: np.ndarray,
    excluded_bands_hz: Sequence[tuple[float, float]],
) -> tuple[
    list[LinearizationFilter],
    list["BoostExclusionDrop"],
    list["BoostExclusionResidual"],
]:
    """Split ``boosts`` into the ones AIMED at an excluded band and the rest.

    **The test is per filter, intrinsic, and relative** — each filter is read
    once against its OWN transfer function, independently of every other, so
    there is no ordering, no iteration and no "drop until it fits". That is
    what separates this from the arbitrary-ordering hazard
    ``interference_nulls.EXCLUSION_CAP_FRACTION`` warns about: this is a
    property of one filter, not a search over subsets.

    **The criterion is the filter's own half-gain bandwidth.** A filter is
    aimed at a band when its realized magnitude anywhere inside that band
    reaches **half its own peak gain in dB** — the standard parametric-EQ
    bandwidth convention (RBJ cookbook defines a bell's bandwidth at its
    half-gain-in-dB points). So the question asked is "does this filter's
    ACTION REGION overlap the band", which is scale-free: a +1 dB bell centred
    in the band is aimed at it and goes; an +11.67 dB bell 0.7 octaves away
    delivering a sixth of its peak into the band is spill and stays.

    **Why not an absolute threshold, in either direction.** Both absolutes
    were tried and both failed, in opposite ways.
    :data:`~jasper.active_speaker.branch_target.SIGNIFICANT_GAIN_DB` (0.5 dB)
    is calibrated for the stopband guard, where any gain outside a
    half-octave-widened passband is anomalous by construction. An excluded
    band sits INSIDE the passband, right next to a dip the fit is working on,
    so nearly any legitimate boost clears 0.5 dB there through its skirts:
    measured, a whole-cascade test at that threshold refused the entire lift
    on **94.4 %** of randomized multi-dip fits and **84.7 %** of single-dip
    ones, destroying a median **14.83 dB** / **8.55 dB** of boost that lived
    almost entirely OUTSIDE the band. Going the other way, no threshold at
    all (a mask on the request) was effectively infinitely permissive. A
    constant borrowed across two different geometries is the bug in both
    directions; the ratio has no such population to be wrong about.

    Returns ``(kept, dropped, residual)``. ``residual`` carries the realized
    max still inside each band AFTER the drops — skirt tails by construction,
    since no surviving filter's action region overlaps the band. It is
    disclosed rather than refused: the post-apply sweep measures what the
    speaker actually did, which is the cheaper and more honest answer than
    refusing a correction on a model.
    """
    kept: list[LinearizationFilter] = []
    dropped: list[BoostExclusionDrop] = []
    band_masks: list[tuple[tuple[float, float], np.ndarray]] = []
    for lo_hz, hi_hz in excluded_bands_hz:
        mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
        if np.any(mask):
            band_masks.append(((float(lo_hz), float(hi_hz)), mask))

    for boost in boosts:
        own_db = 20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response((boost,), grid_hz)), 1e-12,
        ))
        aimed: tuple[tuple[float, float], float] | None = None
        for band_hz, mask in band_masks:
            in_band_db = float(np.max(own_db[mask]))
            if in_band_db >= boost.gain / 2.0 and (
                aimed is None or in_band_db > aimed[1]
            ):
                aimed = (band_hz, in_band_db)
        if aimed is None:
            kept.append(boost)
        else:
            # One record per DROPPED FILTER, naming the band it overlaps most
            # — a filter aimed at two bands is one decision, not two.
            dropped.append(BoostExclusionDrop(
                band_hz=aimed[0], freq_hz=float(boost.freq), q=float(boost.q),
                gain_db=float(boost.gain), realized_in_band_db=aimed[1],
            ))

    # Hoisted: the surviving cascade does not change between bands, and this
    # runs on a Pi.
    surviving_db = (
        20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response(tuple(kept), grid_hz)), 1e-12,
        ))
        if kept else None
    )
    residual = [
        BoostExclusionResidual(
            band_hz=band_hz,
            realized_max_db=(
                float(np.max(surviving_db[mask])) if surviving_db is not None else 0.0
            ),
        )
        for band_hz, mask in band_masks
    ]
    return kept, dropped, residual


def measurement_hole_bands_hz(
    core_bands_hz: Sequence[tuple[float, float] | None],
) -> tuple[tuple[float, float], ...]:
    """The spans NO branch's own measured core band covers (#2599, #2600 item
    4) — the gaps between the supplied :func:`core_level_band_hz` bands, in
    ascending order.

    A two-way speaker is measured one branch at a time, and each branch's core
    band stops where its own crossover hands off. Between the woofer's top and
    the tweeter's bottom there is therefore a span in which NEITHER per-branch
    capture carries trusted evidence, while the SUMMED response there is the
    phase-sensitive blend of both. On the 2026-08-16 round-3 jts3 session that
    span was 1291.4104-2077.2412 Hz: 786 Hz centred at ~1684 Hz, which #2600
    item 4 names as the region "the alignment solve extrapolates across
    exactly the region it must get right".

    A hole is a property of the SET of branches, never of one of them, which
    is why this is a free function the composer calls rather than something
    :func:`fit_driver_linearization` could derive from its own arguments.
    ``None`` entries (a role whose core level could not be read) are skipped:
    a gap can only be named between bands that exist, and inventing one from a
    missing band would be the opposite of evidence. Fewer than two usable
    bands therefore yields ``()`` — no hole is claimed, and the placement rule
    that consumes it is inert. Overlapping or touching bands likewise leave no
    gap between them.
    """
    bands = sorted(
        (float(band[0]), float(band[1]))
        for band in core_bands_hz if band is not None
    )
    holes: list[tuple[float, float]] = []
    reach_hz = -math.inf
    for lo_hz, hi_hz in bands:
        if reach_hz > -math.inf and lo_hz > reach_hz:
            holes.append((reach_hz, lo_hz))
        reach_hz = max(reach_hz, hi_hz)
    return tuple(holes)


def _blind_zone_placements(
    filters: Sequence[LinearizationFilter],
    grid_hz: np.ndarray,
    measured_excess_db: np.ndarray,
    blind_bands_hz: Sequence[tuple[float, float]],
) -> tuple[BlindZonePlacement, ...]:
    """Name every emitted Peaking filter CENTRED in a span no branch measured
    (#2599). Reports; refuses nothing.

    **Run over the FINAL cascade, cuts and lift boosts alike.** The first
    version of this ran over the flattening loop's ``design_peq`` output only,
    and the adversarial gate's 400-fit randomized probe found the hole it left:
    **74 hole-centred positive-gain boosts shipped unnamed**, because the lift
    stage builds its boosts after that point. That is the worst class to miss
    by this function's own argument — a cut in a hole removes level on evidence
    no branch has, but a BOOST adds level into the phase-sensitive blend on the
    same absent evidence, and the lift stage is exactly where a manufactured
    deficit sends one. Reading the emitted list instead of one stage's
    prescriptions makes the universal true by construction: any Peaking filter
    this fit ships whose centre lands in a hole is named, whichever stage
    placed it and whatever its sign.

    **What the disclosure is for.** A filter centred inside a hole is one
    branch acting alone on a region where the per-branch instrument is silent
    for EVERY branch, while the summed response there is the phase-sensitive
    blend of both. The branch's own capture reports its own excess and is
    structurally mute about what removing that excess does to the sum. On the
    2026-08-16 round-3 jts3 run the woofer took a -1.7577 dB cut centred at
    1404.4032 Hz, inside the 1291.4104-2077.2412 Hz hole and inside the
    824-3297 Hz blend window #2600 item 1 says the null detector is
    uncalibrated across; it landed on the blend dip and deepened it, for a net
    woofer EQ of -4.27 dB against a measured worst ripple of -4.33 dB at 1408
    Hz. Nothing in the receipt said a filter had been placed where no
    measurement reached. Now something does, which is precisely the "cheap
    persistence change that would have named the defect a round earlier" that
    #2600 asks for.

    **Why this REPORTS instead of refusing, which was tried first.** A refusal
    needs a rule that separates this cut from legitimate work, and no band
    criterion does. Measured on the repo's own conductor fixture, whose hole is
    1255.8-2020.0 Hz: refusing every centre inside it drops a **-7.821 dB**
    woofer cut at 1708.0 Hz sitting on **+7.821 dB** of the branch's own
    measured excess, a -3.406 dB cut at 1365.7 Hz on +6.28 dB, and the
    tweeter's -2.286 dB cut at 1570.6 Hz — and turns that fixture's PASSING
    correction into ``correction_not_an_improvement`` (before 2.233 dB rms,
    after 2.377, improvement -0.144 against 0.5 required). Clamping to each
    branch's own core band instead is the same rule wearing a different band
    and additionally reverses #1809's measured ruling that a cut past the
    handoff is ordinary useful work: it breaks four further pinned promises,
    among them an 8 dB resonance 0.17 octaves past a woofer's edge.

    The one thing that WOULD separate the two populations is magnitude — this
    defect sat at ~1.76 dB of centre excess and the protected cuts at 2.29,
    6.28 and 7.82 — and inventing a dB threshold in that gap is exactly the
    "constant borrowed across two different geometries" that
    :func:`_boost_exclusion_verdicts` documents as the bug in BOTH directions.
    There is no population behind such a number.

    So the honest separator is not available from inside a single-branch fit:
    it needs the SUM, which only the alignment/crossover layer sees. Until
    that layer owns the blend window, this makes the placement visible rather
    than guessing at it — the disclosure-class posture #2600 itself takes, and
    the "disclose and recommend, never block" ethos ruled 2026-08-14. Hard
    stops stay reserved for the safety class, which a 1.76 dB cut is not.

    **Peaking only, and that is the one scope limit left.** A shelf's ``freq``
    is a CORNER, not a placement: its authority is the whole band to one side
    of it, so asking whether that single frequency sits inside a hole is a
    category error rather than a question with a wrong answer. The shelf and
    the CD-horn Lowshelf backbone are therefore skipped by type, not by stage
    — every Peaking filter from every stage is read.
    """
    if not blind_bands_hz:
        return ()
    placements: list[BlindZonePlacement] = []
    for emitted in filters:
        # Peaking only. A shelf's ``freq`` is a CORNER, not a placement — see
        # the docstring — so it is skipped rather than asked a question its
        # geometry cannot answer.
        if emitted.biquad_type != "Peaking":
            continue
        hole = next(
            (
                (float(lo_hz), float(hi_hz))
                for lo_hz, hi_hz in blind_bands_hz
                if lo_hz <= emitted.freq <= hi_hz
            ),
            None,
        )
        if hole is None:
            continue
        idx = int(np.argmin(np.abs(grid_hz - emitted.freq)))
        placements.append(BlindZonePlacement(
            freq_hz=float(emitted.freq), q=float(emitted.q),
            gain_db=float(emitted.gain),
            blind_band_hz=hole,
            measured_excess_db=float(measured_excess_db[idx]),
        ))
    return tuple(placements)


def _lift_stage(
    grid_hz: np.ndarray,
    working_db: np.ndarray,
    target_curve_db: np.ndarray,
    envelope: EnvelopeCurve,
    band_mask: np.ndarray,
    filters: Sequence[LinearizationFilter],
    vocabulary: FitVocabulary,
    *,
    measured_db: np.ndarray,
    lift_mask: np.ndarray | None = None,
    contribution: np.ndarray | None = None,
    gain_permitted: np.ndarray | None = None,
) -> _Lift:
    """Raise the bands a cut-only fit had to leave dark (PR-L5).

    Runs LAST, on whatever deficit survives the shelf, peaking, and CD-horn
    stages: ``target_curve_db − working_db``, clipped at zero, inside the fit
    band. Two moves, in this order:

    1. :func:`reduce_cuts_for_lift` — shrink our own cuts. Free, no slot, no
       headroom.
    2. Boost filters for the residue, if and only if the vocabulary allows it.

    **Null exclusion still binds.** The desired lift is clamped per bin by
    ``envelope.allowed_depth_db``, the same ceiling the cut side honours — and
    that array is already zero wherever the interference-null registry or the
    position screen excluded a band. So boost cannot fill a measured
    interference null, which is the one thing the owner's ruling kept:
    "null-exclusion stays as a measured fact (registry-gated)". Nothing here
    re-derives that judgement; it consumes it.

    **Inert under a cut-only vocabulary**, and that is not a formality. A
    cuts-only flattening loop deliberately leaves the whole curve at or BELOW
    its target — every dip the driver has is a "deficit" by this stage's
    arithmetic, and the module's standing position is that such a dip is the
    driver's honest natural response, "accepted as the driver's honest natural
    rolloff". Reducing cuts to chase it under a vocabulary that never intended
    to add level would silently change what every pre-PR-L5 caller gets. So
    the whole stage is a lift-vocabulary stage: no boost permission, no lift.

    **Contribution weighting (R10a, #1968).** ``contribution`` is the branch's
    output as a fraction of its own full output. The WANTED deficit is scaled
    by it, so a deficit where the crossover has already taken this branch
    mostly out of the sum attracts proportionally less boost. This is the
    research's "weight the error by each branch's relative contribution... so
    stopband gain has ~zero leverage", applied to the GAIN side only — see
    :mod:`jasper.active_speaker.branch_target` for why cuts are deliberately
    NOT weighted. ``None`` is unweighted, exactly as before R10a.

    **The stopband-gain guard (R10a, #1968) is structural, and it is the one
    thing here that can refuse a cascade the old code emitted.**
    ``gain_permitted`` is the passband widened by half an octave; a realized
    boost cascade putting more than
    :data:`~jasper.active_speaker.branch_target.SIGNIFICANT_GAIN_DB` anywhere
    outside it is refused as ``"stopband_gain"``.

    Why a REALIZED-response check and not a mask on the request: ``lift_mask``
    already confines the *wanted* deficit (and hence every bell's CENTRE) to
    the radiating band, and that is where the old bound stopped. But a bell
    has SKIRTS. A boost centred just inside the radiating edge puts real gain
    a half-octave past it — gain the branch's own crossover then attenuates,
    which is #1809's pathology arriving by a route #1809's mask cannot see.
    **Why the WHOLE grid and not ``band_mask``.** The stage's existing
    realization gate below reads ``realized_db[band_mask]``, and reusing that
    idiom here is the plausible-looking mistake. It is wrong because
    ``band_mask``'s overlap with the stopband is **incidental, not
    guaranteed**: the mask is the fit band
    (:func:`_adaptive_band_trim`'s walk) intersected with the envelope, and
    where that lands depends on the driver's own curve, not on the crossover.

    Measured on the 2026-07-30 JTS3 session, where ONE speaker's two branches
    disagree about it (both arms identical; re-derive with
    ``captures/r10a-objective-20260801/fit_band_probe.py``):

    * **woofer** — fit band ``(150.0, 2747.3)`` Hz against a gain band ending
      at 2266.8 Hz. Its stopband is PARTLY inside the mask: 7 of 78 stopband
      bins, spanning 2323.0-2747.3 Hz, with the rest above the fit band.
    * **tweeter** — fit band ``(2020.0, 15991.5)`` Hz against a gain band
      *starting* at 1764.6 Hz. The fit band begins ABOVE the gain band's lower
      edge, so **all 89** of its stopband bins fall OUTSIDE the mask.
      (Top edge re-derived after #1752 made a term's exact zero a hard
      boundary; it read 18390.9 Hz while the smoother still leaked depth past
      mic-trust's zero. Every other figure in this note is unchanged.)

    So a mask-limited guard would half-see one branch and be completely blind
    on the other, in the same session. That is worse than one that never
    fired: it would look like coverage. Reading the emitted cascade over the
    whole grid has no such dependency.
    ``test_a_mask_limited_guard_would_miss_these_bins_entirely`` pins the
    distinction.

    **The measured-target bound (#2599)** runs on the designed boosts, per
    filter, against ``target_curve_db − measured_db`` — the branch's own
    smoothed measurement, NOT the post-cut ``working_db`` the stage's own
    request is derived from. A boost whose entire action region sits where the
    MEASUREMENT is already at or above target is dropped, its siblings
    survive, and only an empty result carries
    ``"boost_above_measured_target"``. It exists because the realization gate
    below grades against ``envelope.allowed_depth_db``, which across the core
    passband is the 24.0 dB ceiling SENTINEL — a "no limit expressed" marker,
    not a measured allowance, and direction-agnostic besides — and because a
    cut's skirts can manufacture a working-curve deficit the driver does not
    have. See :func:`_boost_evidence_verdicts`.

    **The boost-evidence bound (#1967)** runs last, per filter, on the
    emitted response. ``vocabulary.boost_excluded_bands_hz`` carries the bands
    the composer's cross-position evidence CONTRADICTED boosting at; a boost
    whose own action region overlaps one is DROPPED, its siblings survive, and
    the remaining in-band skirt is disclosed rather than refused
    (:func:`_boost_exclusion_verdicts`). Only when EVERY boost is dropped does
    the lift come back empty with ``"boost_excluded_band"``.

    Suppressed (named, never silent) when no filter slots remain, when
    ``design_peq`` cannot realize the residue, when the realized cascade
    overshoots the envelope's own allowance, when it puts gain in the
    stopband, when every boost it designed acted only where the measurement
    is already at or above target, or when every boost it designed was aimed
    at a boost-excluded band.
    """
    if not vocabulary.allow_boost:
        return _Lift(tuple(filters), 0.0, 0.0, 0.0, "")

    # How much lift is PERMITTED per bin: the distance to target inside the fit
    # band (negative where the curve already sits above it — a cut there may
    # not be unwound), and unconstrained outside it, where this fit makes no
    # claim at all. Read over the whole fit band, NOT ``lift_mask``: a bin the
    # crossover has handed off is still a bin a lifting filter's skirt must
    # not overshoot into.
    headroom_db = np.where(band_mask, target_curve_db - working_db, np.inf)
    # How much lift is WANTED: the positive part of the same distance, bounded
    # per bin by the envelope's allowance. That allowance is a correction-DEPTH
    # ceiling and is direction-agnostic — a bin the measurement cannot support
    # a 3 dB cut at cannot support a 3 dB lift either — and it is already zero
    # wherever the null registry or the position screen excluded a band.
    #
    # ``lift_mask`` narrows WANTED to the driver's own radiating band (#1809),
    # which is what makes the bound boost-only: the cut stages above ran
    # against the full fit band and keep everything they placed. Defaults to
    # the fit band itself, so a caller with no crossover to declare gets the
    # pre-#1809 stage exactly.
    wanted_mask = band_mask if lift_mask is None else lift_mask
    deficit_db = np.clip(
        np.where(wanted_mask, target_curve_db - working_db, 0.0), 0.0, None,
    )
    if contribution is not None:
        # #1968's contribution weighting, gain side only. Scales what the
        # stage ASKS FOR, never what it is allowed to spend — the envelope
        # allowance below stays the measurement's own ceiling.
        deficit_db = deficit_db * np.clip(contribution, 0.0, 1.0)
    wanted = np.minimum(
        deficit_db, np.maximum(envelope.allowed_depth_db, 0.0),
    )
    requested_db = float(np.max(wanted)) if wanted.size else 0.0
    if requested_db < _MIN_FILTER_GAIN_DB:
        return _Lift(tuple(filters), 0.0, 0.0, 0.0, "")

    reduced, delivered = reduce_cuts_for_lift(
        filters, wanted, headroom_db, grid_hz,
    )
    from_reduced_cuts_db = float(np.max(delivered)) if delivered.size else 0.0
    residue = np.clip(wanted - delivered, 0.0, None)
    residue_peak_db = float(np.max(residue)) if residue.size else 0.0
    if residue_peak_db < _MIN_FILTER_GAIN_DB:
        return _Lift(tuple(reduced), requested_db, from_reduced_cuts_db, 0.0, "")

    slots_free = MAX_FILTERS_PER_DRIVER - len(reduced)
    if slots_free <= 0:
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "no_filter_budget",
        )

    # The boost designer's own band is the LIFT band, so a bell's centre can
    # never be placed where this driver has handed off — belt to the braces of
    # a ``residue`` that is already zero out there.
    band_idx = np.flatnonzero(wanted_mask)
    f_low = float(grid_hz[band_idx[0]])
    f_high = float(grid_hz[band_idx[-1]])
    peqs = design_peq(
        np.zeros_like(grid_hz), residue, grid_hz,
        f_low=f_low, f_high=f_high,
        max_filters=slots_free,
        max_cut_db=0.0,
        max_boost_db=min(residue_peak_db, vocabulary.per_filter_boost_cap_db),
        cuts_only=False,
        flatness_target_db=_PEAKING_FLATNESS_TARGET_DB,
        # Explicit, not inherited: this floor is what bounds the #1967 drop
        # radius to +/- 0.68 octaves. See _PEAKING_Q_MIN's own comment.
        q_min=_PEAKING_Q_MIN,
        q_max=_PEAKING_Q_MAX,
        min_filter_gain_db=_MIN_FILTER_GAIN_DB,
    )
    boosts = [
        LinearizationFilter(biquad_type="Peaking", freq=p.freq, q=p.q, gain=p.gain)
        for p in peqs if p.gain > 0.0
    ]
    if not boosts:
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "no_realizable_boost",
        )

    # Realization gate, the same posture ``_hf_continuation_stage`` takes: the
    # cascade that will actually be emitted has to stay inside the envelope's
    # own per-bin allowance. A greedy bell fit can overshoot between its
    # centres, and an overshoot here is a correction claiming permission the
    # measurement never granted it.
    realized_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(tuple(boosts), grid_hz)), 1e-12)
    )
    allowance = np.maximum(envelope.allowed_depth_db, 0.0)
    if np.any(realized_db[band_mask] > allowance[band_mask] + _MIN_FILTER_GAIN_DB):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "exceeds_envelope",
        )

    # #1968's hard rule, enforced on the cascade that will actually be
    # emitted: no significant gain more than half an octave past this
    # branch's acoustic passband edge. Read over the WHOLE grid, NOT
    # ``band_mask`` — the mask's overlap with the stopband is incidental
    # rather than guaranteed, so a mask-limited test has coverage that varies
    # by branch and by session. On the banked 2026-07-30 session it would have
    # seen 7 of the woofer's 78 stopband bins and NONE of the tweeter's 89;
    # see this function's docstring.
    if gain_permitted is not None and np.any(
        realized_db[~gain_permitted] > SIGNIFICANT_GAIN_DB
    ):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "stopband_gain",
        )

    # #2599's measured-target bound. Same posture as #1967's below — per
    # filter, drop-only, on the cascade that will actually be emitted, placed
    # AFTER both whole-cascade gates above so a cascade they refused cannot
    # come back as an accepted subset. The evidence is the branch's own
    # measurement rather than the composer's cross-position verdict, and the
    # question is the opposite direction: not "was boosting here
    # contradicted" but "is there anything here to boost". See
    # :func:`_boost_evidence_verdicts` for why the working curve cannot
    # answer that and for the vacuous gate this closes.
    measured_headroom_db = target_curve_db - np.asarray(
        measured_db, dtype=np.float64,
    )
    boosts, evidence_drops = _boost_evidence_verdicts(
        boosts, grid_hz, measured_headroom_db, band_mask,
    )
    if evidence_drops:
        if not boosts:
            return _Lift(
                tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
                "boost_above_measured_target",
                boost_evidence_drops=tuple(evidence_drops),
            )
        realized_db = 20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response(tuple(boosts), grid_hz)), 1e-12,
        ))

    # #1967's boost-evidence bound, enforced the SAME way and for the same
    # reason: on the cascade that will actually be emitted, over the whole
    # grid, at the same :data:`SIGNIFICANT_GAIN_DB`.
    #
    # **Why not a mask on the request.** Zeroing ``wanted`` inside these bands
    # was the obvious implementation and it is wrong twice, both measured on
    # this stage. It confines bell CENTRES and not their SKIRTS, so
    # ``design_peq`` simply places filters at the band edges and the skirts
    # refill it — on a single dip at a realistic half-depth width it removed
    # 3.66 dB and left +9.93 dB inside the band the evidence contradicted.
    # And it is NOT MONOTONE: this stage's suppressions are all-or-nothing and
    # ``design_peq`` is greedy over the residue, so REMOVING demand can unlock
    # a cascade that was previously refused wholesale — one measured case went
    # from "no boost at all" to a +24.06 dB cascade carrying +12.85 dB MORE
    # gain inside the excluded band. Both pathologies are the one the docstring
    # above already rejects for #1809, arriving by the same route.
    #
    # Reading the realized cascade has neither problem. ``wanted``, the
    # residue and ``design_peq``'s inputs are all untouched, so the cascade
    # designed here is bit-identical with and without this bound and the only
    # thing that can change is whether it is ACCEPTED — which makes the bound
    # monotone by construction: it can never raise the gain at any bin.
    #
    # PER FILTER, not per cascade — see ``_boost_exclusion_verdicts`` for the
    # criterion and for the measured reason a whole-cascade test at an
    # absolute threshold is a ban rather than a bound.
    #
    # **Placed AFTER both gates above, and that ordering is load-bearing.**
    # Dropping filters can only shrink the cascade, so a subset of a cascade
    # those gates already ACCEPTED still satisfies them. Running the drop
    # first would let a cascade they had refused wholesale come back as an
    # accepted subset — the exact unlock pathology this bound was rewritten to
    # eliminate. Monotonicity then holds by construction and by the geometry:
    # every boost bell is non-negative at every bin, so any kept subset's
    # realized response is <= the full cascade's everywhere. Dropping can
    # never raise the gain at any frequency.
    #
    # **Drop-only. No re-spend, no refit.** The freed envelope headroom is not
    # handed back to ``design_peq`` for another pass: bounded, deterministic,
    # nothing to oscillate.
    kept, dropped, residual = _boost_exclusion_verdicts(
        boosts, grid_hz, vocabulary.boost_excluded_bands_hz,
    )
    if dropped:
        if not kept:
            # Every boost was aimed at a contradicted band, so the lift is
            # empty and says why — the whole-lift reason a caller already
            # reads, kept for exactly this case.
            return _Lift(
                tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
                "boost_excluded_band",
                boost_excluded_drops=tuple(dropped),
                boost_excluded_residual=tuple(residual),
                boost_evidence_drops=tuple(evidence_drops),
            )
        boosts = kept
        realized_db = 20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response(tuple(boosts), grid_hz)), 1e-12,
        ))
    # Whatever gain survives INSIDE an excluded band is now skirt tail — no
    # surviving filter's action region overlaps it — and it is disclosed
    # rather than refused. The post-apply sweep measures what the speaker
    # actually did, which is a better answer than refusing a correction on
    # the strength of a model.
    return _Lift(
        tuple([*reduced, *boosts]), requested_db, from_reduced_cuts_db,
        float(np.max(realized_db)), "",
        boost_excluded_drops=tuple(dropped),
        boost_excluded_residual=tuple(residual),
        boost_evidence_drops=tuple(evidence_drops),
    )


#: Every ``lift_suppressed_reason`` a fit can carry — pinned by a test so a
#: new suppression path cannot ship an un-enumerated reason string, exactly
#: like :data:`HF_SUPPRESSION_REASONS`.
LIFT_SUPPRESSION_REASONS: frozenset[str] = frozenset({
    "no_filter_budget",
    "no_realizable_boost",
    "exceeds_envelope",
    "stopband_gain",
    "boost_above_measured_target",
    "boost_excluded_band",
})


def fit_driver_linearization(
    primary: DriverResponse,
    envelope: EnvelopeCurve,
    *,
    vocabulary: FitVocabulary = CUT_ONLY_VOCABULARY,
    radiating_band_hz: tuple[float, float] | None = None,
    blind_bands_hz: Sequence[tuple[float, float]] = (),
    target: BranchTarget | None = None,
) -> LinearizationFit:
    """Fit one driver's linearization from its measured response and
    correction envelope.

    Cut-preferred, not cut-only: ``vocabulary`` decides whether a lift is
    admitted at all, and this function re-proves both that decision and the
    per-filter boost cap on its own output before returning.

    ``envelope`` carries everything besides the raw magnitude curve —
    role, mic tier, driver class, repeat count, and (critically) the
    per-bin allowed correction depth — so this function reads context off
    ``envelope`` rather than taking redundant separate parameters.

    ``vocabulary`` is the allowed-moves input of the topology-agnostic core
    (:class:`FitVocabulary`); it defaults to :data:`CUT_ONLY_VOCABULARY`, so
    a caller that does not opt into boost gets the pre-PR-L5 fit exactly.
    ``radiating_band_hz`` bounds where the fit may add LEVEL to the span this
    driver's own crossover leaves it radiating in
    (:func:`jasper.active_speaker.branch_chain.radiating_band_hz`, which the
    composer solves — this core is handed a band, never a crossover). ``None``
    (the default, and every caller before #1809) means unbounded, which is
    also the honest answer for a one-way box's summed chain.

    It bounds two things at two widths, and the difference between them is the
    whole design. LIFT is bounded at the band ITSELF (#1809): what that is
    about is a driver spending GAIN against its own crossover — the 2026-07-28
    JTS3 woofer carried +11.6155 dB (Q 8) at 2747 Hz, above its own 2 kHz LR4
    crossover, which arrived at +1.06 dB of net acoustic contribution and cost
    11.6 dB of headroom. The SOLVE — the shelf regression, the peaking loop's
    design band, the CD-horn stage's eligibility, and the residual the FIT
    claim is made over — is bounded at the band widened by
    :data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`
    (#2523, :func:`_solve_band_mask`), because a cut in the SHOULDER still
    reaches the sum and a cut 18 dB down the branch's own low-pass does not.
    ``target_level_db``, ``plateau_level_db`` and ``correction_giveback_db``
    are read over the envelope's own region under both bounds and neither
    moves them.

    ``blind_bands_hz`` (#2599) is the separate, cross-branch statement: spans
    NO branch's own measured core band covers, from
    :func:`measurement_hole_bands_hz`. A peaking filter may not be CENTRED in
    one without being NAMED for it — see :func:`_blind_zone_placements` for
    what that disclosure is for, and for the measured reason it reports rather
    than refuses. Empty (the default, every caller before #2599, and every
    session with fewer than two readable core bands) names nothing.

    ``target_level_db`` staying whole-region is a POSITIVE choice, not an
    oversight. It is the LEVEL every stage here grades against — the shelf's
    slope reference, the peaking loop's target array, the adaptive band trim's
    floor, the give-back frame, the residual/verify/observe claims — so it has
    to be derived from the bins the fit may place a filter on. A target read
    over a sub-band would grade the bins outside it against a line nothing
    outside it contributed to. ``driver_core_level_db`` is a different
    question — where does this driver SIT relative to its sibling — and since
    #1929 it reads its median over the radiating band, because only the bins a
    driver radiates in carry that. The two were the same number by
    construction until #1929 proved they were never the same question.

    ``target`` is that level's SHAPE (#1817, R10a) — a
    :class:`~jasper.active_speaker.branch_target.BranchTarget` carrying the
    branch's own committed crossover magnitude, its contribution weight, and
    the band a filter may put GAIN in. Supplied, the stages above grade
    against ``target_level_db + shape`` instead of the flat
    ``target_level_db``, so no filter fights the crossover ANYWHERE rather
    than only outside the radiating band. ``None`` (the default, every caller
    before R10a, every one-way box, and the room-correction lane) is the flat
    target byte for byte.

    **The scalar did not change meaning, and that is what made this
    tractable.** #1817 anticipated a change to what ``target_level_db``
    MEANS — a scalar read by the residual claim, the VERIFY/OBSERVE ladders,
    ``driver_core_level_db`` and ``correction_giveback_db`` — and it would
    have been, had the shape been folded into it. It is not: the shape is
    re-centred to add no level over the very band the scalar is the median of
    (:meth:`~jasper.active_speaker.branch_target.BranchTarget.centred_on`), so
    every one of those consumers reads the same number it always did and only
    the per-bin GRADING moved. Level questions and shape questions stayed
    separate, which is the same seam #1809 and #1929 each found from their own
    side.

    **This fit is independent of the level datum, and that is structural.** A
    driver is flattened to its OWN passband, which is what flattening means;
    where that flattened passband is PLACED relative to the other drivers' is
    a trim, decided later and elsewhere
    (:func:`~jasper.active_speaker.crossover_v2.intervention.anchor_trims`,
    from the raw measured trim). The shared-level-frame offset this
    function used to accept and stamp is deleted with the frame itself, so
    re-placing the pair costs no re-fit — nothing on this side of the seam
    reads a level datum at all.

    Algorithm (design doc "Layer 1a concretely"):
      1. Resample ``primary``'s magnitude onto ``envelope``'s grid, ladder-
         smooth it.
      2. Fit band = envelope-nonzero bins, trimmed by the adaptive-band-trim
         walk (never fit past where the curve has already fallen more than
         one filter's cut budget below target), then narrowed to the SOLVE
         band — ``radiating_band_hz`` widened by
         :data:`~jasper.active_speaker.branch_target.
         STOPBAND_GAIN_MARGIN_OCTAVES`. The LIFT band is that band
         intersected with ``radiating_band_hz`` itself.
      3. Target level = median of the smoothed curve over the trusted core
         passband (NOT the band minimum).
      4. Shelf stage: one cut-only Highshelf if the fit band's regression
         slope rises faster than the threshold, budget-clamped.
      5. Peaking loop: ``jasper.correction.peq.design_peq`` on the
         post-shelf residual, cuts-only, capped per-bin by
         ``min(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)``.
      6. CD-horn compensation stage (``_hf_continuation_stage``, #1668): for a
         driver whose fit band reaches the confidence ceiling, a measured-
         inverse top-octave lift realized cut-only via give-back (a Lowshelf
         backbone + peaking cuts, plus a declared-class continuation policy
         above the ceiling). Gated by repeat agreement and realization
         fit-quality. When it fires, the residual/verify/observe claims are
         computed in the give-back frame (``target_level_db - spend``).
      7. Lift stage (``_lift_stage``, PR-L5): whatever deficit survives steps
         4-6 **inside the lift band** is spent first by SHRINKING this fit's
         own cuts (:func:`reduce_cuts_for_lift`) and then, if ``vocabulary``
         allows it, by boost filters — envelope-bounded per bin, so a measured
         interference null can never be filled, and MEASURED-target bounded
         per filter (#2599), so a boost aimed only at a deficit the cut
         stages manufactured is dropped rather than emitted.
      8. Blind-zone disclosure (#2599, :func:`_blind_zone_placements`):
         every EMITTED Peaking filter — cuts and surviving lift boosts
         alike — whose centre lands inside a ``blind_bands_hz`` span, a
         region no branch's own capture covers, is named in
         :attr:`~LinearizationFit.blind_zone_placements`. Reports only;
         every filter still ships and no claim below moves.

    Returns a :class:`LinearizationFit` with zero filters (an honest no-op)
    when the envelope allows correction nowhere.
    """
    grid_hz = envelope.freqs_hz
    measured_db = np.interp(grid_hz, primary.freqs_hz, primary.magnitude_db)
    smoothed_db = _ladder_smooth(grid_hz, measured_db)

    envelope_mask = envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB
    if not envelope_mask.any():
        return _empty_fit(envelope)

    level_mask = _core_or_fallback_mask(envelope, envelope_mask)
    target_level_db, plateau_level_db = _target_and_plateau_db(smoothed_db, level_mask)

    # The target's SHAPE (#1817). Re-centred on the SAME mask the scalar above
    # is the median of, so it adds shape without moving level — see
    # ``BranchTarget.centred_on``. ``None`` leaves ``target_curve_db`` a flat
    # array at the scalar, which is every pre-R10a caller's exact target.
    #
    # The grid check is explicit and loud because the failure it replaces is
    # neither. A target built on the DRIVER RESPONSE's native grid rather than
    # the ENVELOPE's is the easy mistake — the composer has both in scope —
    # and it surfaced as an IndexError from inside a median several frames
    # down. Same explicit-raise posture as the cut-only and per-filter-cap
    # invariants below: this is hardware-bound output, and `assert` is
    # stripped under `python -O`.
    if target is not None and target.shape_db.shape != grid_hz.shape:
        raise ValueError(
            "BranchTarget was built on a different grid than the envelope: "
            f"target has {target.shape_db.shape[0]} bins, envelope grid has "
            f"{grid_hz.shape[0]}. Build it with `envelope.freqs_hz`."
        )
    centred_target = target.centred_on(level_mask) if target is not None else None
    target_curve_db = (
        np.full_like(grid_hz, target_level_db)
        if centred_target is None
        else centred_target.target_curve_db(target_level_db)
    )

    fit_lo_idx, fit_hi_idx = _adaptive_band_trim(
        grid_hz, smoothed_db, envelope_mask, target_level_db,
    )
    band_mask = np.zeros_like(envelope_mask)
    band_mask[fit_lo_idx:fit_hi_idx + 1] = True
    band_mask &= envelope_mask

    # THE SOLVE BAND (#2523). The fit band, narrowed to where this branch still
    # materially reaches the summed response — see :func:`_solve_band_mask`.
    #
    # **What was wrong, measured.** Since R10a the fit grades against the
    # branch's own IDEAL digital crossover, and a real branch does not follow an
    # ideal crossover into its own deep stopband: it rolls off, then flattens
    # into breakup, leakage and the capture's noise floor. Every dB of that gap
    # arrived at the solve as a demand. On the 2026-08-13 JTS3 run the woofer
    # declared ``[0, 1361.6] Hz`` and the disclosure layer read tens of dB of it
    # at 16 and 20 kHz.
    #
    # Reconstructed in ``test_out_of_band_content_does_not_reach_the_solve`` as
    # a woofer behind a 1600 Hz LR4 whose stopband floors at −14 dB: the
    # pre-#2523 solve spent ALL EIGHT filter slots between 9.7 and 11.8 kHz —
    # including an −11.6 dB Highshelf at 11757.7 Hz — on a branch declared to
    # radiate only to 1282.3 Hz, and reported 19.0133 dB rms of residual for it.
    # The cost is not confined to the claim: that solve returned a
    # ``correction_giveback_db`` of 0.0136 dB, against 2.1668 dB from the SAME
    # unbounded solve on the same fixture WITHOUT the stopband floor — the
    # budget that would have corrected (and so given back) the CORE band went
    # out of band instead. (Under this bound the floored fixture reads 2.1112,
    # i.e. back with its floor-free twin, which is the invariance the test
    # asserts.) That number WAS the SSOT the flow anchored each branch's
    # linearized trim on when this was written, which is what made the whole
    # 2.15 dB land in an emitted trim. Since the 2026-08-19 band fix the anchor
    # measures its own give-back over ``branch_level_bands_hz`` and this number
    # is the audible-band disclosure beside it, so the 2.15 dB no longer reaches
    # a trim by this route. **The defect this paragraph records is unchanged
    # either way**: a solve fed demand it cannot realize spends its slots out of
    # band, and every band-limited reading of the result goes with them — which
    # is the point, and it does not depend on who consumes the reading.
    #
    # **The bound is on what the solver is FED, never on how its output is
    # judged.** Every honesty guard downstream is untouched: the cut-only and
    # per-filter-cap raises, the lift stage's envelope/stopband/boost-exclusion
    # gates, the trim's own sanity margin. A genuine in-band drift rejects
    # exactly as it did.
    #
    # **What this deliberately does NOT narrow.** ``level_mask`` (and so
    # ``target_level_db``, ``plateau_level_db`` and ``correction_giveback_db``)
    # is #1929's question and keeps #1929's answer. ``_observe_octave_summary``
    # stays whole-grid: it is the DISCLOSURE layer, and a stopband the fit no
    # longer solves in is still a stopband a reader is entitled to see. Masking
    # a disclosure is how a fit stops being able to surprise anyone.
    band_mask = _solve_band_mask(grid_hz, band_mask, radiating_band_hz)
    # Re-read the band's edges off the mask the stages will actually run on, so
    # ``design_peq``'s design band, the shelf's regression, the CD-horn stage's
    # eligibility, the VERIFY band and the reported ``fit_band_hz`` cannot
    # disagree with it. A no-op narrowing reproduces the trim's own indices
    # exactly (its endpoints are envelope-mask bins by construction), so every
    # caller that declares no crossover is byte-identical.
    solve_idx = np.flatnonzero(band_mask)
    fit_lo_idx, fit_hi_idx = int(solve_idx[0]), int(solve_idx[-1])

    # Where LIFT may go (#1809) — the fit band clamped to the side of the
    # crossover this driver actually radiates on.
    #
    # **Boosts only, and the asymmetry is the whole design.** A CUT outside a
    # driver's radiating band is ordinary useful work: the crossover has
    # attenuated the band but not silenced it, and whatever leaks through
    # still reaches the summed response. Such a cut is not free of acoustic
    # consequence — it moves the sum, which is the point of placing it — it is
    # free of the two things that make the boost case a defect: it spends no
    # headroom (the charge is a peak, and a cut cannot raise one), and it
    # cannot fight the crossover, because past the edge the curve is already
    # BELOW target and only an overshoot the fit does not place could put it
    # back above. A BOOST there is the pathology #1809 filed. It is
    # attenuated by the same crossover it is fighting (the 2026-07-28 JTS3
    # woofer's +11.6155 dB at 2747 Hz arrived at +1.06 dB of net acoustic
    # contribution), it charges full headroom for that nothing, and it exists
    # only because a branch measured THROUGH its crossover reads the
    # crossover's own rolloff as a driver deficit. A driver must not spend
    # gain fighting its own crossover; it may still stop leaking.
    #
    # **What this bound does NOT fix, deliberately (issue #1817).** Inside the
    # band, the fit is still flattening a crossover-shaped curve toward a FLAT
    # target, so it still lifts the last few dB before the edge: a perfectly
    # flat driver behind an LR4 attracts +2.379 dB at 0.79*Fc. That is bounded
    # by the edge attenuation itself and it is cheap — the same crossover eats
    # 2.27 dB of it, so the branch chain peaks at +0.111 dB and the charge is
    # 1.11 dB, not the 2.4 the filter reads. The real fix is to give the fit a
    # crossover-shaped TARGET rather than a flat one, so no filter fights the
    # crossover anywhere instead of only outside the radiating band; that is a
    # change to what ``target_level_db`` MEANS (a scalar everywhere here:
    # residual, verify, observe, give-back) and does not belong in a bound.
    lift_mask = band_mask
    if radiating_band_hz is not None:
        radiating_lo_hz, radiating_hi_hz = radiating_band_hz
        lift_mask = band_mask & (
            (grid_hz >= radiating_lo_hz) & (grid_hz <= radiating_hi_hz)
        )
    fit_lo_hz = float(grid_hz[fit_lo_idx])
    fit_hi_hz = float(grid_hz[fit_hi_idx])

    filters: list[LinearizationFilter] = []
    working_db = smoothed_db.copy()
    remaining_filters = MAX_FILTERS_PER_DRIVER

    if fit_hi_idx > fit_lo_idx:
        shelf = _shelf_stage(
            grid_hz, smoothed_db, band_mask, fit_lo_hz, fit_hi_hz,
            target_level_db, plateau_level_db,
            shape_db=None if centred_target is None else centred_target.shape_db,
        )
        if shelf is not None:
            working_db = working_db + _highshelf_response_db(
                grid_hz, shelf.freq, shelf.gain, shelf.q,
            )
            filters.append(shelf)
            remaining_filters -= 1

    if remaining_filters > 0 and fit_hi_idx > fit_lo_idx:
        # THE #1817 site. This array was ``np.full_like(grid_hz,
        # target_level_db)`` — a flat line — which is what made a branch
        # measured THROUGH its own crossover read that crossover's rolloff as
        # a driver deficit and attract correction into it. It is now the
        # branch's own crossover shape at the same level.
        per_bin_cap_db = -np.minimum(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)
        peqs = design_peq(
            working_db, target_curve_db, grid_hz,
            f_low=fit_lo_hz, f_high=fit_hi_hz,
            max_filters=remaining_filters,
            max_cut_db=per_bin_cap_db,
            max_boost_db=0.0,
            cuts_only=True,
            flatness_target_db=_PEAKING_FLATNESS_TARGET_DB,
            q_max=_PEAKING_Q_MAX,
            min_filter_gain_db=_MIN_FILTER_GAIN_DB,
        )
        if peqs:
            working_db = working_db + predicted_response(peqs, grid_hz)
            filters.extend(
                LinearizationFilter(
                    biquad_type="Peaking", freq=p.freq, q=p.q, gain=p.gain,
                )
                for p in peqs
            )

    # CD-horn compensation stage (#1668): measured-inverse top-octave lift,
    # realized cut-only via give-back. Runs AFTER the peaking loop so its
    # deficit is measured against the post-flattening working curve.
    hf = _hf_continuation_stage(
        grid_hz, working_db, target_curve_db, target_level_db, plateau_level_db,
        envelope, primary, fit_lo_hz, fit_hi_hz, filters,
    )
    if hf.filters:
        # The Lowshelf backbone goes to position 0 (the emitter's shelf-before-
        # peaks contract); the peaking cuts + optional taper are appended after
        # the flattening peaks (which are all Peaking, so order is preserved).
        filters = [hf.filters[0], *filters, *hf.filters[1:]]
        working_db = working_db + 20.0 * np.log10(
            np.maximum(np.abs(complex_correction_response(hf.filters, grid_hz)), 1e-12)
        )

    # Lift stage (PR-L5): reduce our own cuts, then boost the residue. Runs
    # LAST so its deficit is measured against everything the cut-only stages
    # already achieved — and, when the CD-horn stage fired, in that stage's
    # give-back frame (``target_level_db - hf.spend_db``), which is the level
    # the branch will actually be trimmed back to. Grading the residue against
    # the un-given-back median would ask the lift stage to re-deliver the whole
    # spend the give-back is already returning for free.
    lift = _lift_stage(
        grid_hz, working_db, target_curve_db - hf.spend_db, envelope,
        band_mask, filters, vocabulary,
        # The MEASUREMENT, not the working curve — #2599's bound exists
        # precisely because the two disagree once cuts are placed. Graded in
        # the same give-back frame the stage's own target is, so one frame
        # answers both questions.
        measured_db=smoothed_db,
        lift_mask=lift_mask,
        contribution=None if centred_target is None else centred_target.contribution,
        gain_permitted=(
            None if centred_target is None else centred_target.gain_permitted
        ),
    )
    filters = list(lift.filters)

    # THE #2599 PLACEMENT SITE. Every emitted Peaking filter whose centre lands
    # in a span NO branch's own capture covers is NAMED here. It still ships —
    # see :func:`_blind_zone_placements` for the measured reason a refusal is
    # not available from inside a single-branch fit — so nothing below changes
    # and the cascade is exactly the pre-#2599 one.
    #
    # Read AFTER the lift stage, on the FINAL list, and that placement is the
    # fix for the hole the adversarial gate found: reading the flattening
    # loop's prescriptions instead let 74 hole-centred lift BOOSTS ship unnamed
    # across a 400-fit probe, which is the worst class to miss — a boost adds
    # level into the blend on evidence no branch has. Reading the emitted list
    # also reports each cut's FINAL gain, after any shrink
    # ``reduce_cuts_for_lift`` applied, rather than the gain it was designed
    # with.
    #
    # Graded against ``target_curve_db``, NOT the lift stage's give-back frame:
    # this number answers "how far above its own target does the measurement
    # sit here", which is the branch's own excess and is the frame the
    # flattening loop that placed most of these filters was working in.
    blind_zone_placements = _blind_zone_placements(
        filters, grid_hz, smoothed_db - target_curve_db, blind_bands_hz,
    )

    # THE CLAIM SEAM (R10b; first-principles panel CC-2(b), "rebuild
    # ``working_db`` from ``complex_correction_response`` unconditionally" —
    # ``captures/first-principles-panel-20260731/objective-verdict.md``).
    # Everything below this line is a REPORTED NUMBER — residual, verify,
    # observe, give-back — and every one of them is graded here against the
    # cascade the graph will actually emit.
    #
    # Rebuilding from ``smoothed_db`` plus the WHOLE cascade rather than
    # carrying the incremental ``working_db`` forward does two jobs at once:
    #
    #  1. It cannot double-count. The lift stage can SHRINK a cut already
    #     folded in above, so an incremental update would apply that filter
    #     twice. (This was the rebuild's original and only job, and it fired
    #     only when the lift stage changed the filter list.)
    #  2. It cannot grade an approximation. The peaking stage folds itself in
    #     with :func:`jasper.correction.peq.predicted_response`, whose
    #     ``_bell_response_db`` is a Lorentzian in log-frequency — its
    #     half-width matches the RBJ peaking biquad, but "the far skirts are
    #     still a Lorentzian approximation" (that function's own docstring).
    #     :func:`complex_correction_response` is the exact biquad, shared with
    #     the emitter's headroom charge and the runtime contract's proof.
    #
    # Job 2 is why this is unconditional. Before R10b the rebuild lived inside
    # ``if lift.filters != tuple(filters):``, so a cut-only vocabulary — which
    # makes the lift stage inert by design, and is what every pre-PR-L5 caller
    # gets — reported residuals computed against the Lorentzian. That is the
    # shelf-Q defect class of 2026-07-27 arriving in peaking form: the fit
    # grading itself with an evaluator the hardware does not use. See
    # :mod:`jasper.active_speaker.delta_probe`'s module docstring — "a model
    # cannot audit itself".
    #
    # STAGE-INTERNAL arithmetic is deliberately left alone. A search heuristic
    # picking its next peak off an approximate residual is a fit-quality
    # question; a CLAIM is a correctness one. The two are separated here, not
    # conflated: this rebuild is the last write to ``working_db``, no
    # filter-producing stage runs after it, and so it cannot move a single
    # emitted filter on any path — only the numbers reported about them.
    #
    # WHAT THIS SEAM DOES NOT REACH (issue #2013), because a seam that
    # overstates itself is the defect it was built to fix. It makes the claim
    # CURVE exact. It does not make the claim FRAME exact: ``frame_target_db``
    # below is ``target_curve_db - hf.spend_db``, and ``hf.spend_db`` was sized
    # by ``_hf_continuation_stage`` ABOVE this line, against the peaking stage's
    # Lorentzian-folded ``working_db``. That stage's ``fit_quality`` suppression
    # is a DECISION taken on the same approximate curve (its realization check
    # is exact, but the ``cut_target_db`` it checks against is not), and
    # ``measured_deficit_at_ceiling_db`` is reported from it.
    #
    # Measured, not assumed — banked 2026-07-30 JTS3 session, exact-fold
    # counterfactual in ``captures/r10b-alignment-20260801/
    # lorentzian_gap_probe.py``: the spend moves 0.143 dB (3.2418 -> 3.0987) and
    # the committed trim up to 0.162 dB, which is LARGER than anything this
    # seam itself moves. The suppression verdict did not flip on any of the 8
    # rows — "not shown to flip on this corpus", not "shown safe". Moving that
    # stage's input changes what it DECIDES, which needs its own evidence and
    # its own review; #2013 owns it.
    #
    # This note used to say that one of the numbers below is not report-only,
    # because ``correction_giveback_db`` was the SSOT the crossover_v2 anchor
    # placed each branch's linearized TRIM on. **That is no longer true**: the
    # anchor measures its own give-back over ``branch_level_bands_hz`` and this
    # number is now the audible-band disclosure beside it, so grading it
    # exactly no longer moves an emitted trim.
    #
    # The ARGUMENT survives the move intact, which is why it is kept rather
    # than deleted. It applies now to the level-band give-back, which does place
    # the trim and which is computed from the same realized cascade
    # (``complex_correction_response`` over the emitted filters, not the
    # Lorentzian) — so "grade the biquads you actually ship" is still a claim
    # about an emitted trim, just at a different seam. The magnitudes recorded
    # below are the ones that motivated it — measured on the banked 2026-07-30
    # JTS3 session at up to 0.124 dB of give-back and
    # 0.040 dB of committed trim (cut-only vocabulary;
    # ``captures/r10b-alignment-20260801/lorentzian_gap_probe.py``). That is the
    # anchor becoming correct, not a new degree of freedom: the give-back's
    # definition has always been "the level this cascade removes", and it now
    # measures the cascade rather than a model of it.
    working_db = smoothed_db + 20.0 * np.log10(
        np.maximum(
            np.abs(complex_correction_response(tuple(filters), grid_hz)), 1e-12
        )
    )

    # N1 (adversarial review, 2026-07-24): an explicit raise, not a bare
    # `assert` -- this is a safety invariant on HARDWARE-BOUND output (a
    # filter here eventually reaches a real driver's EQ), and `assert` is
    # stripped entirely under `python -O`. A future bug in the shelf/PEQ/
    # CD-horn/lift stages above must still be caught in every runtime mode,
    # not just an unoptimized one.
    #
    # PR-L5 made the invariant conditional on the VOCABULARY rather than
    # unconditional. It did not weaken it: a cut-only vocabulary — every
    # caller that does not explicitly ask for boost, including every pre-PR-L5
    # one — is held to exactly the same raise as before.
    if not vocabulary.allow_boost and any(f.gain > 0.0 for f in filters):
        raise RuntimeError("linearization fit emitted a boost under a cut-only vocabulary")

    # Per-filter caps are HARD invariants on every emitted filter, and the
    # total spend/boost can legitimately exceed them (see
    # MAX_NORMALIZATION_SPEND_DB and PER_FILTER_BOOST_CAP_DB), so re-prove them
    # here rather than trusting each stage's own clamp — same explicit-raise
    # posture as the cut-only check.
    if any(f.gain < -PER_FILTER_CUT_CAP_DB - 1e-6 for f in filters):
        raise RuntimeError("linearization fit exceeded the per-filter cut cap")
    if any(
        f.gain > vocabulary.per_filter_boost_cap_db + 1e-6 for f in filters
    ):
        raise RuntimeError("linearization fit exceeded the per-filter boost cap")

    # The give-back this driver's correction actually removed from its own
    # reference (core) band — the SSOT for the AUDIBLE-BAND question, published
    # as ``core_band_giveback_db``. It does not anchor a trim; the anchor
    # measures its own give-back over ``branch_level_bands_hz``.
    # The MEASURED before-vs-after core-band level delta: the power-domain band
    # average of the curve BEFORE correction minus the same average AFTER
    # (``working_db`` is ``smoothed_db`` plus the cascade). Averaging the
    # CORRECTION alone instead would be power-domain-approximate — exact only
    # for a flat core band, and up to ~1.1 dB under-return on a 12 dB-tilted
    # (woofer-shaped) core, because a power-domain mean of the correction cannot
    # know which bins carry the level it is being subtracted from. This
    # formulation is exact by definition of the quantity it measures: it IS the
    # level change of THIS branch's own core band. (It used to be described as
    # "the band whose level the anchor restores" — that band is now
    # ``branch_level_bands_hz``, which the anchor measures for itself; this is
    # the audible-band answer, exact for the band it names.)
    correction_giveback_db = 0.0
    if filters:
        correction_giveback_db = (
            _power_band_average_db(smoothed_db, level_mask)
            - _power_band_average_db(working_db, level_mask)
        )

    # Give-back frame: when the CD-horn stage fired it cut the whole band by
    # `spend` so the flow's trim re-solve levels the branches back — so the
    # honest reference for the residual/verify/observe claims is the target
    # curve MINUS spend, not the original one. `hf.spend_db` is 0 when the
    # stage did not fire, so untouched paths keep the original frame. The
    # `target_level_db` FIELD still reports the original median.
    #
    # A CURVE since R10a (#1817): the frame is the target's shape shifted by a
    # scalar spend, so the give-back rides on top of the crossover shape
    # rather than replacing it. Flat when no ``target`` was supplied.
    frame_target_db = target_curve_db - hf.spend_db
    residual = (working_db - frame_target_db)[band_mask]
    residual_rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    residual_max_db = float(np.max(np.abs(residual))) if residual.size else 0.0

    # Honesty-ladder levels 2/3 (#1668 PR-D) — see LinearizationFit's own
    # docstring. Computed over the SAME post-filter working_db and give-back
    # frame the FIT claim above used, just wider/full-range bands.
    verify_band_hz, verify_residual_rms_db, verify_residual_max_db = (
        _verify_band_and_residual(
            grid_hz, working_db, frame_target_db, fit_lo_hz, fit_hi_hz,
        )
    )
    observe_octave_summary = _observe_octave_summary(
        grid_hz, working_db, frame_target_db,
    )

    # Reason summary: octave centers ABOVE the confidence ceiling are disclosed
    # as beyond-measurement-confidence when the CD-horn stage fired (their
    # relative lift is a declared-class continuation, not a measured claim).
    reason_summary = _octave_band_reason_summary(envelope)
    if hf.filters:
        reason_summary = {
            center: (
                ReasonCode.BEYOND_MEASUREMENT_CONFIDENCE.value
                if float(center) > hf.ceiling_hz else code
            )
            for center, code in reason_summary.items()
        }

    return LinearizationFit(
        role=envelope.role,
        filters=tuple(filters),
        fit_band_hz=(fit_lo_hz, fit_hi_hz),
        target_level_db=target_level_db,
        residual_rms_db=residual_rms_db,
        residual_max_db=residual_max_db,
        reason_summary=reason_summary,
        mic_tier=envelope.mic_tier,
        driver_class=envelope.driver_class,
        n_repeats=envelope.n_repeats,
        verify_band_hz=verify_band_hz,
        verify_residual_rms_db=verify_residual_rms_db,
        verify_residual_max_db=verify_residual_max_db,
        observe_octave_summary=observe_octave_summary,
        hf_continuation_spend_db=hf.spend_db,
        hf_continuation_ceiling_hz=hf.ceiling_hz,
        hf_continuation_policy=hf.policy,
        hf_continuation_suppressed_reason=hf.suppressed_reason,
        measured_deficit_at_ceiling_db=hf.measured_deficit_at_ceiling_db,
        correction_giveback_db=correction_giveback_db,
        # headroom_cost_db is deliberately left at its 0.0 default: the charge
        # is a property of the emitted branch chain, which this core does not
        # know. See the field's own comment — the composer stamps it.
        lift_requested_db=lift.requested_db,
        lift_from_reduced_cuts_db=lift.from_reduced_cuts_db,
        lift_from_boost_db=lift.from_boost_db,
        lift_suppressed_reason=lift.suppressed_reason,
        lift_boost_excluded_drops=lift.boost_excluded_drops,
        lift_boost_excluded_residual=lift.boost_excluded_residual,
        lift_boost_evidence_drops=lift.boost_evidence_drops,
        blind_zone_placements=blind_zone_placements,
    )
