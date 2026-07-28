# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Layer-1a driver-linearization fit engine (#1668 PR-C).

Consumes ONE driver's :class:`~jasper.audio_measurement.program_analysis.
DriverResponse` (the primary, gated, calibrated measurement) plus its
:class:`~jasper.active_speaker.linearization_envelope.EnvelopeCurve` (from
:func:`jasper.active_speaker.linearization_envelope.compose_envelope`) and
produces a cut-only PEQ/shelf fit that flattens the driver toward a
per-session target level, honoring the envelope's per-bin correction-depth
ceiling everywhere. Pure computation: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:func:`jasper.correction.peq.design_peq` (the existing greedy cuts-only PEQ
designer, extended here — backward-compatibly — to accept a per-bin cut
ceiling). No I/O, no CamillaDSP emission — this module answers "what filters
would flatten this driver," nothing more. Wiring the result into the v2
conductor's candidate and the eventual APPLY emission stage are separate
concerns (the conductor wiring is this same PR; APPLY emission is later).

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
tweeter protection remain untouched hard rails. A vocabulary that forbids
boost still enforces the cut-only invariant with an explicit ``raise`` before
returning (not a bare ``assert`` — a hardware-bound safety invariant must
survive ``python -O``; see :func:`fit_driver_linearization`), pinned by a test.

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
from jasper.sound.profile import (
    RESPONSE_SAMPLE_RATE_HZ, FilterSpec, _filter_response_complex, _freq_trig,
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
# and the flow's ANCHORED trim give-back (crossover_v2_flow._fit_linearization)
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
# report finding 5). LOCKSTEP with the flow layer's
# ``crossover_v2_flow.LINEARIZATION_MIN_PAIRED_OCCURRENCES`` (3); kept as a
# local constant rather than imported because the flow imports THIS module
# (a fit->flow import would be a cycle), mirroring this module's other
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

    ``allow_boost`` is the evidence gate. The conductor grants it only for a
    session that will actually run the delta probe, so an unverified boost
    claim cannot reach a driver — which is the whole condition under which the
    engineering spec's boost mode was allowed to exist at all.
    """

    allow_boost: bool = False
    #: Per-filter boost ceiling. TOTAL boost is uncapped by design (owner
    #: ruling); this bounds one biquad's realization, not the correction.
    per_filter_boost_cap_db: float = PER_FILTER_BOOST_CAP_DB

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_boost": self.allow_boost,
            "per_filter_boost_cap_db": self.per_filter_boost_cap_db,
        }


#: The pre-PR-L5 posture, and the default: cuts only. Every existing caller
#: that does not pass a vocabulary gets exactly the fit it got before this
#: capability existed, byte for byte.
CUT_ONLY_VOCABULARY = FitVocabulary()


@dataclass(frozen=True)
class SharedLevelFrame:
    """ONE level frame both (or all) drivers' targets are expressed in.

    **The defect this closes.** Before PR-L5 each driver was fitted to a flat
    target at ITS OWN median (:func:`_target_and_plateau_db`) and the two
    numbers had no stated relationship: on the 2026-07-27 JTS3 profile the
    tweeter's was −7.24 dB and the woofer's −21.13 dB, 13.9 dB apart, and the
    only stage that leveled the pair was the overlap-band trim — which was
    itself carrying ~11 dB of frame error (PR-L3). PR-L4 added an ASSERTION
    that the realized branch levels agree; this makes them agree by
    construction, which is the difference between catching the bug and not
    having it.

    **How.** Each role proposes the system-referred level its own passband
    would land at: ``core_level_db[role] + trim_db[role]``. The frame adopts
    the LOUDEST proposal and derives every role's target from it, so
    ``target[role] + trim[role]`` is one identical number for every role.
    :attr:`offset_db` is what each role must move to get there.

    **Why the loudest, not the mean or the quietest.** The quietest reference
    cuts every other driver purely to match a dark one — spending real max SPL
    to reach a level nothing wanted; the mean does half of that. The loudest
    moves only the driver that is actually out of place, which is the owner's
    "a 4 dB natural darkness gets its 4 dB" posture, and — crucially — that
    move is realized in the TRIM, by reducing an attenuation the speaker was
    already applying. It costs no headroom at all. This is the trim-domain
    face of "reduce our own cuts before you boost".

    **What it trusts.** ``trim_db`` is the measured branch trim, so a broken
    trim propagates into the frame. That is the same input PR-L3 fixed and
    PR-L4's measured-vs-datasheet cross-check and realized-level assertion
    both grade; this frame does not add a second opinion, it removes a place
    where two opinions could silently coexist.
    """

    system_level_db: float
    reference_role: str
    target_level_db: Mapping[str, float]
    offset_db: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_level_db": self.system_level_db,
            "reference_role": self.reference_role,
            "target_level_db": dict(self.target_level_db),
            "offset_db": dict(self.offset_db),
        }


def solve_shared_level_frame(
    core_level_db: Mapping[str, float], trim_db: Mapping[str, float],
) -> SharedLevelFrame:
    """Reconcile every role's own passband level into one shared frame.

    ``core_level_db`` is each role's own core-passband level (what
    :func:`driver_core_level_db` reads off the same curve the fit targets) and
    ``trim_db`` its branch attenuation. Roles missing from ``trim_db`` are
    read as 0 dB of trim.

    Works for any number of roles — one (a passive box's summed chain, where
    the frame is trivially that chain's own level and the offset is 0), two,
    or N. Raises ``ValueError`` on an empty mapping rather than inventing a
    frame from nothing.
    """
    if not core_level_db:
        raise ValueError("a shared level frame needs at least one role")
    proposals = {
        role: float(level) + float(trim_db.get(role, 0.0))
        for role, level in core_level_db.items()
    }
    reference_role = max(proposals, key=lambda r: (proposals[r], r))
    system_level_db = proposals[reference_role]
    targets = {
        role: system_level_db - float(trim_db.get(role, 0.0))
        for role in core_level_db
    }
    return SharedLevelFrame(
        system_level_db=system_level_db,
        reference_role=reference_role,
        target_level_db=targets,
        offset_db={
            role: targets[role] - float(level)
            for role, level in core_level_db.items()
        },
    )


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
    gain: float  # dB; always <= 0 (cut-only invariant)

    def to_dict(self) -> dict[str, float | str]:
        return {
            "biquad_type": self.biquad_type,
            "freq": self.freq,
            "q": self.q,
            "gain": self.gain,
        }


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
    # This is the SSOT for the trim give-back: ``crossover_v2_flow.
    # _fit_linearization`` anchors each branch's linearized trim at
    # ``raw_trim + correction_giveback_db``, returning the branch's audible band
    # to its pre-correction system level — no solver prediction, no overlap-band
    # averaging (measured live on JTS3 2026-07-24: the overlap-band route
    # returned only 5.81 dB of a 9.27 dB spend, because the tweeter's LR4 skirt
    # power-weights that average toward the least-cut region and the shelf's
    # wide RBJ transition is not at full depth there).
    #
    # Note the realization fit-quality gate bounds the correction's SHAPE only
    # over [onset, ceiling]; below the onset the realized cascade may diverge
    # from cut_target (e.g. a clamped shelf the residual only partly absorbs).
    # That is safe here precisely because the anchor consumes this MEASURED
    # delta: an under-realized plateau shrinks the achieved lift, it never
    # breaks the leveling.
    #
    # Computed for EVERY fit that emitted filters (0.0 when none), so a woofer
    # carrying only flattening cuts anchors correctly too. When the CD-horn
    # stage fires this reads ≈ spend + the flattening peaks' own in-band share.
    correction_giveback_db: float = 0.0
    # --- PR-L5 disclosure ------------------------------------------------
    # "This correction costs N dB of maximum level." The PEAK positive dB of
    # the emitted cascade over the fit grid — literally how far the program
    # must be attenuated ahead of this branch for the boosted band to stay
    # under 0 dBFS, and therefore exactly the max-SPL the correction spends.
    # The emitter folds it into ``active_baseline_headroom`` (the same gain
    # room-correction boost already rides), so the ceiling is never breached;
    # this field is the number a household and a journal reader are owed.
    # 0.0 for every cut-only fit — which is every fit before PR-L5.
    headroom_cost_db: float = 0.0
    # How far this driver had to move to reach the session's SHARED level
    # frame (:class:`SharedLevelFrame`), positive = it was the dark one and
    # needs lifting. Consumed by ``crossover_v2_flow._fit_linearization``,
    # which adds it into the anchored trim so every branch realizes the same
    # system level BY CONSTRUCTION rather than by later comparison. 0.0 when
    # no frame was supplied (every pre-PR-L5 caller) and for whichever role
    # the frame took as its reference.
    level_frame_offset_db: float = 0.0
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "filters": [f.to_dict() for f in self.filters],
            "fit_band_hz": list(self.fit_band_hz),
            "target_level_db": self.target_level_db,
            "residual_rms_db": self.residual_rms_db,
            "residual_max_db": self.residual_max_db,
            "reason_summary": dict(self.reason_summary),
            "mic_tier": self.mic_tier,
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
            "level_frame_offset_db": self.level_frame_offset_db,
            "lift_requested_db": self.lift_requested_db,
            "lift_from_reduced_cuts_db": self.lift_from_reduced_cuts_db,
            "lift_from_boost_db": self.lift_from_boost_db,
            "lift_suppressed_reason": self.lift_suppressed_reason,
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
    it to ~0.5 dB. So the conductor's linearized-branch model
    (:func:`jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor.
    _fit_linearization` — the trim re-solve, the ripple-optimal scan, and the
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
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    # One trig table (at RESPONSE_SAMPLE_RATE_HZ) shared across every biquad in
    # the cascade — the same reuse _filter_response_db's own callers do.
    trig = _freq_trig(freqs)
    total = np.ones(freqs.shape, dtype=np.complex128)
    for f in filters:
        # LinearizationFilter and FilterSpec are structurally the same biquad
        # record (biquad_type/freq/gain/q); FilterSpec is the declared input of
        # the shared profile evaluator.
        spec = FilterSpec(
            name="linearization", biquad_type=f.biquad_type,
            freq=f.freq, gain=f.gain, q=f.q,
        )
        total = total * np.array(_filter_response_complex(spec, freqs, trig))
    return total


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


def _power_band_average_db(magnitude_db: np.ndarray, mask: np.ndarray) -> float:
    """Power-domain band average of ``magnitude_db`` over ``mask``:
    ``10*log10(mean(10**(dB/10)))``.

    PARITY DUPLICATE of ``jasper.audio_measurement.program_analysis.
    _band_average_db``'s averaging semantics (module-private there — see this
    module's top docstring for the no-cross-module-private-imports convention),
    evaluated against a boolean mask on this module's own fit grid rather than
    a (lo, hi) frequency pair. LOCKSTEP REQUIREMENT: this MUST stay the same
    power-domain mean the trim solver uses, because
    :attr:`LinearizationFit.correction_giveback_db` is consumed by
    ``crossover_v2_flow._fit_linearization`` as the level a branch's own
    correction removed — if the two averaging domains disagreed, the anchored
    trim would systematically mis-level the branch (a linear-dB mean here
    would read ~0.3 dB different on a non-flat correction).

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
    target_level_db: float,
    fit_lo_hz: float,
    fit_hi_hz: float,
) -> tuple[tuple[float, float], float, float]:
    """The honesty ladder's VERIFY level: the SAME residual math the fit
    claim itself uses (post-filter ``working_db`` vs the fit's own
    ``target_level_db``), evaluated over a band extending roughly an octave
    PAST the fit band's own top — ``[fit_lo_hz, min(2*fit_hi_hz,
    grid_hz[-1])]``. Report-only (see :class:`LinearizationFit`'s docstring).
    """
    verify_hi_hz = min(2.0 * fit_hi_hz, float(grid_hz[-1]))
    verify_band_hz = (fit_lo_hz, verify_hi_hz)
    verify_mask = (grid_hz >= fit_lo_hz) & (grid_hz <= verify_hi_hz)
    residual = (working_db - target_level_db)[verify_mask]
    rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    max_db = float(np.max(np.abs(residual))) if residual.size else 0.0
    return verify_band_hz, rms_db, max_db


def _observe_octave_summary(
    grid_hz: np.ndarray, working_db: np.ndarray, target_level_db: float,
) -> dict[str, float]:
    """The honesty ladder's OBSERVE level: per-octave achieved-vs-target
    magnitude to the grid's own top (20 kHz on the production grid),
    independent of the fit/verify bands — the disclosure layer (see
    :class:`LinearizationFit`'s docstring). Mirrors
    :func:`_octave_band_reason_summary`'s own octave-center sampling
    (same :data:`_OCTAVE_BAND_CENTERS_HZ`, same "nearest grid bin" pick,
    same range guard), so the two dicts key identically band-for-band.
    """
    out: dict[str, float] = {}
    for center in _OCTAVE_BAND_CENTERS_HZ:
        if center < grid_hz[0] or center > grid_hz[-1]:
            continue
        idx = int(np.argmin(np.abs(grid_hz - center)))
        out[str(int(center))] = float(working_db[idx] - target_level_db)
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
) -> float | None:
    """One driver's own core-passband level — the number a
    :class:`SharedLevelFrame` reconciles across drivers (PR-L5).

    Deliberately NOT a second estimator. It runs the identical resample →
    ladder-smooth → core-mask → median chain :func:`fit_driver_linearization`
    runs, and returns the same ``target_level_db`` that fit would have chosen
    on its own, so a frame built from this and a fit built from that cannot
    disagree about where a driver sits. It exists as a separate entry point
    only because the frame has to be solved across ALL drivers before any one
    of them is fitted.

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
    level_mask = _core_or_fallback_mask(envelope, envelope_mask)
    return _target_and_plateau_db(smoothed_db, level_mask)[0]


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
) -> LinearizationFilter | None:
    """Fit ONE cut-only Highshelf if the fit band's smoothed slope rises
    faster than :data:`SHELF_SLOPE_THRESHOLD_DB_PER_OCT`. Returns ``None``
    when no shelf is warranted (falling/shallow slope, too few points to
    regress, or the normalization budget leaves nothing to spend).

    Dormant for falling-slope drivers by design — a cut-only shelf cannot
    correct a naturally falling response; the deferred Lowshelf counterpart
    for that case is documented in the comment block above this function.
    """
    if int(band_mask.sum()) < 2:
        return None
    log2_f = np.log2(grid_hz[band_mask])
    slope_db_per_oct, intercept = np.polyfit(log2_f, smoothed_db[band_mask], 1)
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
    consistent with ``crossover_v2_flow.LINEARIZATION_MIN_PAIRED_OCCURRENCES``)
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
    deficit_db = target_level_db - working_db
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

# Float-noise slack (dB) on the permitted-headroom test. The constraint itself
# is exact — a shrink may not raise any bin past what ``headroom_db`` permits —
# so this is only here to keep bisection from rejecting a shrink over the last
# bit at the end of a float. Matches :data:`_ENVELOPE_NONZERO_EPS_DB`'s role.
_CUT_REDUCTION_EPS_DB: float = 1e-6


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


def _lift_stage(
    grid_hz: np.ndarray,
    working_db: np.ndarray,
    target_level_db: float,
    envelope: EnvelopeCurve,
    band_mask: np.ndarray,
    filters: Sequence[LinearizationFilter],
    vocabulary: FitVocabulary,
) -> _Lift:
    """Raise the bands a cut-only fit had to leave dark (PR-L5).

    Runs LAST, on whatever deficit survives the shelf, peaking, and CD-horn
    stages: ``target_level_db − working_db``, clipped at zero, inside the fit
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

    Suppressed (named, never silent) when no filter slots remain, when
    ``design_peq`` cannot realize the residue, or when the realized cascade
    overshoots the envelope's own allowance.
    """
    if not vocabulary.allow_boost:
        return _Lift(tuple(filters), 0.0, 0.0, 0.0, "")

    # How much lift is PERMITTED per bin: the distance to target inside the fit
    # band (negative where the curve already sits above it — a cut there may
    # not be unwound), and unconstrained outside it, where this fit makes no
    # claim at all.
    headroom_db = np.where(band_mask, target_level_db - working_db, np.inf)
    # How much lift is WANTED: the positive part of the same distance, bounded
    # per bin by the envelope's allowance. That allowance is a correction-DEPTH
    # ceiling and is direction-agnostic — a bin the measurement cannot support
    # a 3 dB cut at cannot support a 3 dB lift either — and it is already zero
    # wherever the null registry or the position screen excluded a band.
    wanted = np.minimum(
        np.clip(np.where(band_mask, target_level_db - working_db, 0.0), 0.0, None),
        np.maximum(envelope.allowed_depth_db, 0.0),
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

    band_idx = np.flatnonzero(band_mask)
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
    return _Lift(
        tuple([*reduced, *boosts]), requested_db, from_reduced_cuts_db,
        float(np.max(realized_db)), "",
    )


#: Every ``lift_suppressed_reason`` a fit can carry — pinned by a test so a
#: new suppression path cannot ship an un-enumerated reason string, exactly
#: like :data:`HF_SUPPRESSION_REASONS`.
LIFT_SUPPRESSION_REASONS: frozenset[str] = frozenset({
    "no_filter_budget",
    "no_realizable_boost",
    "exceeds_envelope",
})


def fit_driver_linearization(
    primary: DriverResponse,
    envelope: EnvelopeCurve,
    *,
    vocabulary: FitVocabulary = CUT_ONLY_VOCABULARY,
    level_frame: SharedLevelFrame | None = None,
) -> LinearizationFit:
    """Fit one driver's cut-only linearization from its measured response
    and correction envelope.

    ``envelope`` carries everything besides the raw magnitude curve —
    role, mic tier, driver class, repeat count, and (critically) the
    per-bin allowed correction depth — so this function reads context off
    ``envelope`` rather than taking redundant separate parameters.

    ``vocabulary`` is the allowed-moves input of the topology-agnostic core
    (:class:`FitVocabulary`); it defaults to :data:`CUT_ONLY_VOCABULARY`, so
    a caller that does not opt into boost gets the pre-PR-L5 fit exactly.
    ``level_frame`` is the session's :class:`SharedLevelFrame`; when supplied,
    this driver's :attr:`~LinearizationFit.level_frame_offset_db` reports how
    far it must move to reach that shared frame, which the conductor folds
    into the anchored trim. The frame does NOT change what this function
    flattens toward — a driver is flattened to its own passband, which is what
    flattening means — it changes where that flattened passband is placed
    relative to the other drivers', and that placement is a trim.

    Algorithm (design doc "Layer 1a concretely"):
      1. Resample ``primary``'s magnitude onto ``envelope``'s grid, ladder-
         smooth it.
      2. Fit band = envelope-nonzero bins, trimmed by the adaptive-band-trim
         walk (never fit past where the curve has already fallen more than
         one filter's cut budget below target).
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
         4-6 is spent first by SHRINKING this fit's own cuts
         (:func:`reduce_cuts_for_lift`) and then, if ``vocabulary`` allows it,
         by boost filters — envelope-bounded per bin, so a measured
         interference null can never be filled.

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

    fit_lo_idx, fit_hi_idx = _adaptive_band_trim(
        grid_hz, smoothed_db, envelope_mask, target_level_db,
    )
    band_mask = np.zeros_like(envelope_mask)
    band_mask[fit_lo_idx:fit_hi_idx + 1] = True
    band_mask &= envelope_mask
    fit_lo_hz = float(grid_hz[fit_lo_idx])
    fit_hi_hz = float(grid_hz[fit_hi_idx])

    filters: list[LinearizationFilter] = []
    working_db = smoothed_db.copy()
    remaining_filters = MAX_FILTERS_PER_DRIVER

    if fit_hi_idx > fit_lo_idx:
        shelf = _shelf_stage(
            grid_hz, smoothed_db, band_mask, fit_lo_hz, fit_hi_hz,
            target_level_db, plateau_level_db,
        )
        if shelf is not None:
            working_db = working_db + _highshelf_response_db(
                grid_hz, shelf.freq, shelf.gain, shelf.q,
            )
            filters.append(shelf)
            remaining_filters -= 1

    if remaining_filters > 0 and fit_hi_idx > fit_lo_idx:
        target_array = np.full_like(grid_hz, target_level_db)
        per_bin_cap_db = -np.minimum(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)
        peqs = design_peq(
            working_db, target_array, grid_hz,
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
        grid_hz, working_db, target_level_db, plateau_level_db,
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
        grid_hz, working_db, target_level_db - hf.spend_db, envelope,
        band_mask, filters, vocabulary,
    )
    if lift.filters != tuple(filters):
        filters = list(lift.filters)
        # Rebuild ``working_db`` from the smoothed measurement and the WHOLE
        # post-lift cascade rather than adding a delta: the stage can shrink a
        # filter already folded into ``working_db``, so an incremental update
        # would double-count it.
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
    # reference (core) band — the SSOT the flow anchors its linearized trim on.
    # The MEASURED before-vs-after core-band level delta: the power-domain band
    # average of the curve BEFORE correction minus the same average AFTER
    # (``working_db`` is ``smoothed_db`` plus the cascade). Averaging the
    # CORRECTION alone instead would be power-domain-approximate — exact only
    # for a flat core band, and up to ~1.1 dB under-return on a 12 dB-tilted
    # (woofer-shaped) core, because a power-domain mean of the correction cannot
    # know which bins carry the level it is being subtracted from. This
    # formulation is exact by definition of the quantity being restored: it IS
    # the level change of the band whose level the anchor restores.
    correction_giveback_db = 0.0
    if filters:
        correction_giveback_db = (
            _power_band_average_db(smoothed_db, level_mask)
            - _power_band_average_db(working_db, level_mask)
        )

    # Give-back frame: when the CD-horn stage fired it cut the whole band by
    # `spend` so the flow's trim re-solve levels the branches back — so the
    # honest "flat" reference for the residual/verify/observe claims is
    # `target_level_db - spend`, not the original median. `hf.spend_db` is 0
    # when the stage did not fire, so untouched paths keep the original frame.
    # The `target_level_db` FIELD still reports the original median.
    frame_target_db = target_level_db - hf.spend_db
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

    # "This correction costs N dB of maximum level" (PR-L5). The peak positive
    # dB of the emitted cascade — nothing when the fit is cut-only, which keeps
    # every pre-PR-L5 fit's disclosure at a literal 0.0.
    headroom_cost_db = 0.0
    if any(f.gain > 0.0 for f in filters):
        cascade_db = 20.0 * np.log10(
            np.maximum(
                np.abs(complex_correction_response(tuple(filters), grid_hz)), 1e-12
            )
        )
        headroom_cost_db = float(max(0.0, float(np.max(cascade_db))))

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
        headroom_cost_db=headroom_cost_db,
        level_frame_offset_db=(
            float(level_frame.offset_db.get(envelope.role, 0.0))
            if level_frame is not None else 0.0
        ),
        lift_requested_db=lift.requested_db,
        lift_from_reduced_cuts_db=lift.from_reduced_cuts_db,
        lift_from_boost_db=lift.from_boost_db,
        lift_suppressed_reason=lift.suppressed_reason,
    )
