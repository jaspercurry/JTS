# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Layer-1a driver-linearization fit engine (#1668 PR-C).

Consumes ONE driver's ``DriverResponse`` plus its ``EnvelopeCurve`` and
produces a cut-PREFERRED PEQ/shelf fit that flattens the driver toward a
per-session target level, honoring the envelope's per-bin correction-depth
ceiling everywhere. Pure computation: no I/O, no CamillaDSP emission.

The allowed vocabulary is an INPUT, not a hardcode (:class:`FitVocabulary`):
nothing about the speaker's topology reaches this core, so do not add a
way-count, a role branch or a crossover assumption here. Boost is uncapped in
total and evidence-gated — what bounds it is the closed-loop delta probe, and
its headroom cost is disclosed and absorbed by the emitter's
``active_baseline_headroom``. Lift is bounded to the driver's radiating side
of its crossover (#1809); the SOLVE runs over that band widened by
:data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`
(#2523), because a cut in the shoulder still reaches the sum and a demand from
the deep stopband is one no cascade can answer. The fit domain is whatever
grid the caller's envelope was composed on.

See docs/active-speaker-tuning-layers-design.md "Layer 1a concretely".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.camilla_config_contract import SHELF_Q as _HIGHSHELF_Q
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
    _ladder_smooth,
)

# --------------------------------------------------------------------------- #
# fitting policy constants
# --------------------------------------------------------------------------- #

# Per-filter cut ceiling, dB. Shared by the shelf stage and the peaking loop's
# per-bin cap array — design doc "cuts generous (-12 dB, Q<=8)".
PER_FILTER_CUT_CAP_DB: float = 12.0

# A bound on TOTAL normalization spend across the whole fit — how far below the
# driver's own core-passband peak the fit may settle. Every dB cut here is a dB
# of max-SPL headroom the corrected driver gives up, so left unbounded a driver
# with a naturally rolling-off passband could have the fit chase that rolloff
# arbitrarily deep.
#
# 18 dB covers the measured JTS3 tweeter deficit (14.2-14.3 dB at the
# reference-tier confidence ceiling) with margin; at 12 the correction was
# budget-bound below the measured trend. The spend is a max-SPL LEDGER cost,
# not a listening-level cost, and it is disclosed
# (``correction_giveback_db``, ``hf_continuation_spend_db``).
#
# The spend can exceed PER_FILTER_CUT_CAP_DB: no single filter may, so the
# CD-horn stage clamps its Lowshelf backbone at the per-filter cap and lets the
# peaking residual absorb the rest. Enforcement: a stage's spend is clamped to
# the REMAINING budget (this constant minus the plain plateau-vs-target gap the
# core already spends). ``target_level_db`` itself is left UNCLAMPED.
MAX_NORMALIZATION_SPEND_DB: float = 18.0

# Linear-regression slope (dB per octave, over log2(f)) above which the fit band
# is treated as a genuine tilted shelf shape rather than local ripple. Only a
# RISING slope fires the shelf stage: cut-only fitting cannot correct a
# naturally falling response, so a falling slope is left to the peaking loop.
SHELF_SLOPE_THRESHOLD_DB_PER_OCT: float = 3.0

# Hard cap on filters per driver (shelf + peaking combined) — design doc
# "Fitting policy" via the engineering-spec build-order.
MAX_FILTERS_PER_DRIVER: int = 8

# Per-filter BOOST ceiling, dB (PR-L5) — the mirror of PER_FILTER_CUT_CAP_DB,
# and deliberately the same number.
#
# A REALIZATION bound, not a policy cap, which is why it survives the owner's
# "arbitrary gain caps GO" ruling while the total stays uncapped: one RBJ
# biquad asked for +12 dB already has a Q-dependent transition wide enough to be
# doing something other than what the fit drew. TOTAL boost stays unbounded
# because a cascade composes — a deeper deficit gets more filters.
PER_FILTER_BOOST_CAP_DB: float = 12.0

# A bin below this allowed-depth is treated as "the envelope permits nothing
# here" (float noise or a taper's asymptotic tail, not a real allowance).
_ENVELOPE_NONZERO_EPS_DB: float = 0.05

# Below this magnitude a filter is cosmetic — mirrors design_peq's own default
# ``min_filter_gain_db``, kept LOCAL because it also gates the shelf stage's own
# worth-adding check. Revisit this mirror if design_peq's default moves.
_MIN_FILTER_GAIN_DB: float = 0.5

_PEAKING_Q_MAX: float = 8.0

# The narrowest a BOOST bell may be. Passed EXPLICITLY at the lift stage's
# ``design_peq`` call even though it equals that function's own default, because
# it is load-bearing for a safety property in THIS module (#1967) and an
# inherited default is not a bound this module controls.
#
# :func:`_boost_exclusion_verdicts` drops a boost when an excluded band lies
# inside the filter's own half-gain bandwidth, and for a peaking biquad that
# bandwidth is a function of Q alone — so the Q floor IS the drop radius:
# Q 1.0 -> +/-0.68 octaves (shipped), Q 0.5 -> +/-1.25, Q 0.3 -> +/-1.85.
# Lowering this widens every drop decision by the same factor; if a future
# tuning needs broader boost bells, re-derive the drop criterion in the same PR.
_PEAKING_Q_MIN: float = 1.0
_PEAKING_FLATNESS_TARGET_DB: float = 1.0

# The RBJ Highshelf's fixed Butterworth Q, imported above from
# ``camilla_config_contract.SHELF_Q`` (the one source). The APPLY stage spells
# this SAME number into the emitted shelf's CamillaDSP ``q`` field, so the
# modeled response this module subtracts — and the realization gate, residual
# and VERIFY prediction built on it — is the response the speaker realizes.
# Keep the emitted parameter and this constant in lockstep;
# ``tests/test_sound_peq_response.py`` pins them to CamillaDSP's slope<->Q
# formula. At ``slope: 6`` the realized Q depends on the shelf's gain and
# collapses to 0.476 at -11 dB, which is the 2026-07-27 shelf-Q defect.

# Octave-band centers for the candidate artifact's compact reason summary — an
# octave-band summary, not a per-bin dump.
_OCTAVE_BAND_CENTERS_HZ: tuple[float, ...] = (
    250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 12000.0, 16000.0, 20000.0,
)

# --------------------------------------------------------------------------- #
# CD-horn compensation stage constants (#1668)
# --------------------------------------------------------------------------- #

# Low bound of the CD-horn continuation stage's compensation/agreement band.
# The measured top-octave deficit is expressed RELATIVE to the driver's trusted
# 4-8 kHz band, so agreement is checked from 4 kHz to the confidence ceiling.
HF_COMPENSATION_BAND_LO_HZ: float = 4_000.0

# Repeat-agreement gate (objective suppression). Per-bin spread (max-min across
# the capture's repeat sweeps, ladder-smoothed) over the compensation band must
# stay under these limits or the stage is suppressed. Two tiers because
# measurement noise grows with frequency. Sourced from the owner's per-serial
# UMIK-2 uncertainty research: the stock-cal protocol's own uncertainty is
# ~+/-1.5 dB @12 kHz / +/-2.3 dB @16 kHz, so repeats disagreeing by more than
# these tighter limits mean THIS capture is noisier than the protocol baseline.
HF_AGREEMENT_LIMIT_LOW_DB: float = 1.0
HF_AGREEMENT_LIMIT_HIGH_DB: float = 2.0

# The frequency (Hz) below which HF_AGREEMENT_LIMIT_LOW_DB applies (and at/
# above which HF_AGREEMENT_LIMIT_HIGH_DB applies) within the agreement band.
_HF_AGREEMENT_TIER_SPLIT_HZ: float = 10_000.0

# Minimum sweep occurrences (primary + repeats) the agreement gate needs — the
# N>=3-total "paired gate" (sigma-seeding report finding 5). LOCKSTEP with
# ``crossover_v2.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES``; local
# rather than imported because that module imports this one.
_HF_MIN_OCCURRENCES: int = 3

# The closed vocabulary of CD-horn continuation suppression reasons. Pinned by a
# test so a new suppression path cannot ship an un-enumerated reason string.
HF_SUPPRESSION_REASONS: frozenset[str] = frozenset({
    "insufficient_repeats",
    "repeat_disagreement",
    "fit_quality",
    "no_filter_budget",
})

# Max magnitude error (dB) tolerated between the realized cut-domain cascade and
# the desired cut_target over [onset, ceiling]. Above this the whole stage is
# suppressed (reason="fit_quality") rather than ship a mis-shaped correction.
#
# 2.0 rather than 1.5: on real (ragged) curves the tighter bar also caught
# ordinary curve raggedness, passing a 9.27 dB spend by only 0.2 dB. An isolated
# 1.5-2.0 dB excursion at the smoothing scale is measurement texture, not a
# shape failure; the worst mis-shape reachable in review probing measured 2.23
# and is still caught.
HF_REALIZATION_TOLERANCE_DB: float = 2.0

# Ceiling on the CD-horn spend imposed by the SINGLE-Lowshelf realization, dB —
# independent of, and binding below, the MAX_NORMALIZATION_SPEND_DB ledger.
# Measured live on JTS3 2026-07-24: the realization passes the quality gate at
# spend 11.27 and fails from ~11.9 upward; 11.0 leaves margin under that cliff.
#
# It caps how much lift ONE shelf can deliver, not how much the driver needs, so
# raising the ledger budget alone buys no more correction. The last few dB
# toward true tabletop need a different REALIZATION (stacked shelves, or literal
# boost), not a bigger number here.
HF_SINGLE_SHELF_SPEND_CAP_DB: float = 11.0

# Flatness target for the CD-horn stage's own residual peaking fit — TIGHTER
# than the flattening loop's because this fit tracks a SHAPED target and is then
# judged by HF_REALIZATION_TOLERANCE_DB. With the loose 1.0 the RMS over the
# mostly-matched fit band diluted the still-unfitted shelf-transition error and
# design_peq stopped early: 2 of 7 slots, 2.18 dB residual, suppressed. At 0.5
# the same case uses 5 slots and lands at 1.27 dB.
_HF_RESIDUAL_FLATNESS_TARGET_DB: float = 0.5

# Max cut (dB) of the "taper" continuation policy's single trailing Highshelf.
# Above the confidence ceiling nothing is measurable, so for breakup-prone or
# unknown driver tops the stage walks the relative lift back DOWN by
# min(spend/2, this). Capped at 6 dB so the taper protects the unseen top
# without itself becoming a large unverified move.
HF_TAPER_MAX_DB: float = 6.0

# Where the taper's corner sits above the confidence ceiling when there is room:
# far enough up that the shelf's transition stays in unmeasured territory, close
# enough that it still bites inside the band.
_HF_TAPER_CORNER_RATIO: float = 1.25

# The taper's own corner must stay strictly below Nyquist or CamillaDSP's biquad
# check refuses the WHOLE config at load. Derived from the runtime contract's
# own sample rate — never a fresh literal — so a rate change cannot drift this
# from what ``camilla_yaml._validated_biquad_entry`` enforces.
_HF_TAPER_NYQUIST_HZ: float = DEFAULT_SAMPLE_RATE / 2.0

# Continuation policy above the confidence ceiling, keyed by DECLARED driver
# class — the driver class's ONLY remaining authority over the CD-horn stage
# (sizing is class-blind, from measurement). "hold": nothing extra above the
# ceiling, for drivers whose top is trusted to keep extending. "taper": append
# one trailing Highshelf CUT walking the lift back down above the band we cannot
# see, for a rising-breakup metal dome and for an UNKNOWN driver. Keys MUST
# cover DRIVER_CLASSES exactly (pinned by a test).
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
    the topology-agnostic fit core (PR-L5).

    Deliberately small: every field is a move the fit can MAKE, not a fact about
    the speaker. Way count, driver roles, pad authority and alignment are the
    composer's business and are spelled nowhere in this module. ``allow_boost``
    is the evidence gate — the v2 session grants it only when the commission
    will actually run the delta probe.
    """

    allow_boost: bool = False
    #: Per-filter boost ceiling. TOTAL boost is uncapped by design (owner
    #: ruling); this bounds one biquad's realization, not the correction.
    per_filter_boost_cap_db: float = PER_FILTER_BOOST_CAP_DB
    #: Bands no LIFT filter may be AIMED at (#1967). Enforced per filter on the
    #: emitted response — a boost is dropped when the band falls inside its own
    #: half-gain bandwidth — never as a whole-cascade veto. Cuts are untouched,
    #: which is why it lives here rather than in the envelope's
    #: ``allowed_depth_db``: that array is direction-agnostic and zeroing it
    #: would forbid a legitimate cut at the same bins. The composer supplies
    #: bands its evidence positively CONTRADICTS boosting at; empty is "nothing
    #: contradicted", not "no evidence available".
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_boost": self.allow_boost,
            "per_filter_boost_cap_db": self.per_filter_boost_cap_db,
            "boost_excluded_bands_hz": [
                [float(lo), float(hi)] for lo, hi in self.boost_excluded_bands_hz
            ],
        }


#: The pre-PR-L5 posture, and the default: cuts only. Every caller that does not
#: pass a vocabulary gets the fit it got before this capability existed.
CUT_ONLY_VOCABULARY = FitVocabulary()


@dataclass(frozen=True)
class BoostExclusionDrop:
    """One boost filter the #1967 bound removed, and the arithmetic that removed
    it — so a reader can re-derive the decision instead of trusting it.

    ``realized_in_band_db`` is this filter's OWN realized magnitude at its
    strongest point inside ``band_hz``; it was dropped because that reached at
    least ``gain_db / 2``, i.e. the band lies inside its half-gain bandwidth.
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

    Skirt tail by construction: no surviving filter's action region overlaps the
    band. Refusing on it would be refusing a correction on the strength of a
    model; the post-apply sweep measures the reality instead.
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

    ``action_band_hz`` is the span of this filter's own half-gain bandwidth that
    the fit makes a claim over; ``measured_excess_db`` is how far the branch's
    own smoothed MEASURED response sits ABOVE the fit's target at the least-hot
    bin in that span. Dropped because that is ``>= 0`` everywhere it acts.
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
    """One peaking filter the fit CENTRED in a span no branch's own measurement
    covers (#2599). A disclosure, not a refusal — the filter ships.

    ``blind_band_hz`` is the hole it landed in; ``measured_excess_db`` is
    ``smoothed - target`` at that centre, which is what makes the record
    actionable. A large excess is a real driver feature the crossover is not
    hiding (act on Fc or order); a small one is the fit shaping a blend it
    cannot see.
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


def _highshelf_response_db(
    freqs_hz: np.ndarray, corner_hz: float, gain_db: float, q: float,
) -> np.ndarray:
    """RBJ Audio EQ Cookbook Highshelf magnitude response, in dB, at ``freqs_hz``
    for a filter designed at ``corner_hz``/``gain_db``/``q``.

    The same digital biquad family CamillaDSP realizes, at
    :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`. A separate
    implementation from ``sound.profile._filter_response_db`` because the
    interfaces differ: that one dispatches across every biquad type for
    single-filter lookups and returns ``list[float]``; this is Highshelf-only,
    vectorized, and returns an ndarray — the shape this fit loop needs.
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
    (not :class:`jasper.correction.peq.PEQ`, which has no ``biquad_type`` and is
    always implicitly Peaking).
    """

    biquad_type: str  # "Peaking" | "Highshelf" | "Lowshelf"
    freq: float
    q: float
    # dB; may be positive, up to PER_FILTER_BOOST_CAP_DB.
    gain: float

    def to_dict(self) -> dict[str, float | str]:
        return {
            "biquad_type": self.biquad_type,
            "freq": self.freq,
            "q": self.q,
            "gain": self.gain,
        }


#: The persisted key naming WHICH MICROPHONE measured the round this entry came
#: from. A named constant because a second writer legitimately emits it
#: (``driver_prescription_to_candidate_fields`` carries it onto a prescribed
#: branch) and its reader ``CrossoverV2Session._mic_trust_ceiling_hz`` decides
#: where the delta probe may grade — a spelling drift would silently remove that
#: ceiling rather than fail (#2649).
MIC_TIER_FIELD = "mic_tier"


@dataclass(frozen=True)
class LinearizationFit:
    """One driver's fitted linearization — the Layer-1a artifact.

    ``fit_band_hz == (0.0, 0.0)`` signals no fit was attempted (the envelope
    allowed correction nowhere); ``filters`` is empty and the other fields still
    carry honest degenerate values.

    ``verify_*`` and ``observe_octave_summary`` are the honesty ladder's other
    two levels. FIT claims accuracy strictly inside the envelope-allowed,
    adaptively-trimmed band; VERIFY applies the SAME residual math roughly an
    octave past that band's top; OBSERVE is per-octave achieved-vs-target to the
    grid's own top. All are REPORT-ONLY — nothing gates on them.

    Every one of those numbers is measured on the REALIZED cascade
    (:func:`complex_correction_response`, the exact RBJ biquads CamillaDSP
    emits), never the Lorentzian the peaking search uses internally. The FRAME
    still carries one pre-seam term — ``frame_target_db = target_curve_db -
    hf.spend_db``, and ``hf.spend_db`` is sized against a ``working_db`` the
    seam has not rebuilt yet, worth up to 0.143 dB of spend on the banked
    2026-07-30 session. #2013 owns closing it.

    All three ladder levels are FIT DIAGNOSTICS, computed per-driver from the
    single design-axis MEASURE capture. None is the flat-linearization spec
    claim, which is graded on the spatially-combined cloud curve by
    :func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`. The two answer
    different questions on different curves and will legitimately disagree.
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
    # "did-not-fire" state so a driver that never runs the stage serializes
    # byte-identically to before it existed.
    # ``measured_deficit_at_ceiling_db`` reports the UNCAPPED measured deficit
    # at the ceiling, so a budget-bound partial correction stays visible.
    hf_continuation_spend_db: float = 0.0
    hf_continuation_ceiling_hz: float = 0.0
    hf_continuation_policy: str = ""
    hf_continuation_suppressed_reason: str = ""
    measured_deficit_at_ceiling_db: float = 0.0
    # How much LEVEL this driver's correction removed from its own reference
    # (core) band, POSITIVE dB — the MEASURED before-vs-after power-domain band
    # average over ``_core_or_fallback_mask``. Exact by definition of the
    # quantity: it is the level change of the very band whose level the anchor
    # puts back. (Averaging the CORRECTION alone would be power-domain
    # approximate — up to ~1.1 dB under-return on a 12 dB-tilted core.)
    #
    # The SSOT for the AUDIBLE-BAND give-back. **It does NOT place the trim:**
    # ``crossover_v2.intervention.plan_linearization`` anchors on a give-back
    # measured over ``branch_level_bands_hz``, because a give-back spent against
    # a trim must be measured in that trim's frame. Using this one to place the
    # trim shipped the jts3 horn tweeter 3.67 dB hot (2026-08-19).
    #
    # Computed for EVERY fit that emitted filters (0.0 when none).
    correction_giveback_db: float = 0.0
    # --- PR-L5 disclosure, #1808 charge ----------------------------------
    # "This correction costs N dB of maximum level" — the realized peak of the
    # branch chain this fit is emitted into (``crossover ⊗ linearization ⊗
    # trim``) plus ``branch_chain.HEADROOM_MARGIN_DB``, which is exactly what
    # the emitter CHARGES to ``active_baseline_headroom``.
    #
    # **Stamped by the composer, not computed here**: a correction's cost is a
    # property of the chain it is emitted into, which this topology-agnostic
    # core deliberately does not know. A fit evaluated with no branch honestly
    # reports 0.0 — no branch, no charge.
    headroom_cost_db: float = 0.0
    # The lift the boost vocabulary was asked for and what it delivered, dB, over
    # the fit band — non-zero only when the lift stage fired.
    # ``lift_from_reduced_cuts_db`` is the share bought by SHRINKING this fit's
    # own cuts rather than by adding gain; it is free.
    lift_requested_db: float = 0.0
    lift_from_reduced_cuts_db: float = 0.0
    lift_from_boost_db: float = 0.0
    lift_suppressed_reason: str = ""
    # #1967's boost-evidence bound, disclosed at the two levels it decides at:
    # one record per boost filter REMOVED because its own action region
    # overlapped a contradicted band, and what the surviving cascade still puts
    # inside each of those bands (accepted skirt, not a refusal). A whole-lift
    # refusal additionally sets ``lift_suppressed_reason``; a partial one does
    # NOT, because a lift did happen.
    lift_boost_excluded_drops: tuple[BoostExclusionDrop, ...] = ()
    lift_boost_excluded_residual: tuple[BoostExclusionResidual, ...] = ()
    # #2599's two measured-evidence bounds, disclosed the same way.
    #
    # ``lift_boost_evidence_drops``: one record per boost filter removed because
    # the branch's own MEASURED response was already at or above target
    # everywhere that filter acts. Only an empty result sets
    # ``lift_suppressed_reason`` to ``"boost_above_measured_target"``.
    #
    # ``blind_zone_placements``: one record per EMITTED Peaking filter whose
    # centre lands inside a span no branch's own capture covers, read off the
    # FINAL cascade so the universal holds by construction. Removes nothing —
    # the filter ships and is NAMED (the #2600 disclosure class).
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

    The emitted biquads rotate phase near their corners, and a branch
    correction is SUMMED through the crossover, which is phase-dominated. A
    zero-phase magnitude model therefore mispredicts the summed response and can
    land FURTHER from it than omitting the filters entirely: measured on JTS3
    (#1667), 2.0 dB mistracking against 1.7 dB for a no-correction model, where
    this complex model tracks to ~0.5 dB. There is no zero-phase
    branch-correction path.

    Every entry is the exact RBJ biquad CamillaDSP realizes, via
    :func:`jasper.sound.profile._filter_response_complex`, IMPORTED rather than
    re-derived so the phase and magnitude of the applied correction can never
    silently disagree with the emitted graph. Callers apply it in the LINEAR
    domain: ``W_lin = W * complex_correction_response(...)``. A thin role
    adapter over :func:`jasper.active_speaker.branch_chain.chain_response`,
    shared with the emitter's headroom charge and the runtime contract's proof.
    """
    return chain_response([f.to_dict() for f in filters], freqs_hz)


def linearization_filters_by_role(
    linearization_mapping: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Reduce a persisted ``{role: LinearizationFit.to_dict()}`` mapping down to
    the emitter's own reduced input shape: ``{role: [filter_dict, ...]}``.

    Shared by the two RICH-candidate call sites that thread a persisted
    linearization result into ``emit_active_speaker_baseline_config``.

    ``baseline_profile.recompose_applied_baseline_yaml`` deliberately does NOT
    call this helper: its snapshot's ``"linearization"`` key is already in this
    function's OUTPUT shape, and calling this on an already-reduced mapping
    silently returns ``{}`` for every role rather than raising. Do not
    "consolidate" that seam onto this helper —
    ``test_linearization_filters_by_role_on_already_reduced_shape_is_empty``
    pins the trap.

    Defensive, not authoritative: era-tolerant absence is the caller's job, and
    the emitter's ``_validated_linearization`` is the fail-closed gate.
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
# The charge's derivation changed twice, and the stamp is NOT re-derived on load
# (docs/historical/linearization-campaign-2026-07.md, "Cross-era disclosure"):
# it is a record of what that graph was emitted with, and a recommission
# replaces it. So the era must travel WITH the number.
#
# Era 1 was the SUM of a fit's positive filter gains — a loose upper bound; on
# the 2026-07-28 JTS3 profile 22.458 dB against a +4.00 dB realized peak. Era 2
# is that realized peak. **#2758 opened a THIRD era whose direction is new**:
# the grid the peak is evaluated on now spans the whole domain, so a stamp made
# under the narrower grid can read SMALLER than re-emitting the identical
# filters charges today (1.8596 stamped against 7.8305 charged).
#
# The era is recorded, never inferred: nothing on a persisted fit distinguishes
# the rules, so sniffing would be a guess dressed as a fact. Absent means
# UNKNOWN, never a default.
HEADROOM_COST_BASIS_REALIZED_PEAK = "realized_peak"
HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN = "realized_peak_full_domain"
HEADROOM_COST_BASIS_UNKNOWN = "unknown"


def worst_headroom_cost_db(linearization_mapping: Mapping[str, Any]) -> float:
    """The max-level cost of a whole correction, dB — the WORST branch's
    :attr:`LinearizationFit.headroom_cost_db` (PR-L5).

    Worst branch and not the sum, matching
    ``camilla_yaml.linearization_headroom_db``: the driver chains run in
    PARALLEL after the split, so no single sample path sees two branches'
    boosts and the graph gives up the largest one.

    Takes a persisted mapping, so it is defensive the same way
    :func:`linearization_filters_by_role` is: a malformed or era-older entry is
    skipped rather than raising. Defined once because both the session's
    candidate payload and the web layer's ``_candidate_summary`` disclose it.
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

    Same averaging semantics as ``program_analysis._band_average_db``, kept
    separate because the interfaces differ: this takes a boolean mask on this
    module's own grid and returns 0.0 on an empty mask rather than raising. The
    same power-domain mean the trim solver uses, so
    :attr:`LinearizationFit.correction_giveback_db` and the level-band give-back
    published beside it stay directly comparable.
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
        # Honesty-ladder levels 2/3 are degenerate placeholders here too: no fit
        # was attempted, so there is nothing to verify and no honest target to
        # compare an observation against.
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
    """The honesty ladder's VERIFY level: the SAME residual math the fit claim
    uses, over ``[fit_lo_hz, min(2*fit_hi_hz, grid_top)]`` — roughly an octave
    past the fit band's own top. Report-only.

    ``target_curve_db`` is per-bin since R10a (#1817): a flat array at
    ``target_level_db`` reproduces the pre-R10a number exactly, and the
    crossover-shaped curve stops this band — which runs straight through the
    handoff — from scoring a branch's own crossover rolloff as residual.
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
    magnitude to the grid's own top, independent of the fit/verify bands.

    Mirrors :func:`_octave_band_reason_summary`'s octave-center sampling, so the
    two dicts key identically band-for-band. ``target_curve_db`` is per-bin
    since R10a; this band runs to the WHOLE grid, so on a two-way branch most of
    its octaves sit in a stopband where a flat target reported the crossover's
    own attenuation as a deficit of tens of dB.
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
    """The "core passband" — bins where BOTH mic-trust and class-prior still sit
    at the ceiling sentinel — intersected with the fit-eligible mask. Falls back
    to the whole fit-eligible mask when the core is empty (an aggressively
    tapered tier or class with no untapered region at all).
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
#: This fit ladder-smooths at 1/6 octave below 4 kHz, so bins closer together
#: than that are not independent samples. A third of an octave is two smoothing
#: kernels — the narrowest span on which "the median" is a statistic rather than
#: one smoothed point wearing a statistic's name (~8 bins on the production
#: grid).
#:
#: It protects against a DISCONTINUITY, not a wrong answer: measured on a flat
#: tweeter, the raw intersection read −2.847 dB from a single bin at LR2 Fc 3750
#: and −19.401 dB from the whole-mask fallback at Fc 3775 — 16.55 dB across
#: 25 Hz of crossover frequency, into a gate whose tolerance is 3.0 dB.
#:
#: **DISCARDING a sub-floor intersection is the wrong way**: falling back to the
#: whole core mask MOVES the cliff into ordinary two-way territory (LR4
#: 3625->3650 stepped 33.84 dB). So a sub-floor intersection is WIDENED to
#: exactly this width instead, downward from its own top edge, and if the core
#: mask's bottom stops that short the deficit is made up UPWARD from the core's
#: bottom bin. Both directions are needed because the two roles run out of room
#: at opposite ends — a tweeter's intersection is pinned against the core mask's
#: TOP, a woofer's slides DOWN into a raised room gate. The result is continuous
#: by construction. Two cases still take the whole mask, and
#: :func:`core_level_band_hz` discloses both: a genuinely EMPTY intersection,
#: and a core mask itself narrower than the floor.
_MIN_LEVEL_BAND_OCTAVES: float = 1.0 / 3.0


def _spans_floor(lo_hz: float, hi_hz: float) -> bool:
    """Is ``[lo_hz, hi_hz]`` at least :data:`_MIN_LEVEL_BAND_OCTAVES` wide?

    One predicate, so the question is asked identically of the raw intersection
    and of each widened candidate — asking it two ways is how the first version
    of this floor came to be silently inactive on the woofer side.
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

    An EMPTY intersection falls back to the whole core mask: a three-way mid
    squeezed between two crossovers honestly has no radiating band, and a level
    read over a wider-than-ideal band is still a measured level, where dropping
    it would let one squeezed role stop grading every other one. That is the one
    path that changes what the number MEANS, which is what
    :func:`core_level_band_hz` exists to disclose.
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

    # Sub-floor. Widening runs on the core mask's OWN bins, and each edge is
    # snapped OUTWARD to the first bin that actually reaches the floor width —
    # snapping inward leaves the result a bin short and silently sends every
    # widened band to the whole-mask fallback.
    core_idx = np.flatnonzero(core)
    core_freqs = grid_hz[core_idx]
    span = 2.0 ** _MIN_LEVEL_BAND_OCTAVES

    # DOWN from this intersection's own top edge first. Anchoring on ``hi_used``
    # rather than on the crossover is what makes the neighbourhood continuous.
    # This is the tweeter case: the room below is its passband.
    top = int(np.searchsorted(core_freqs, hi_used, side="right")) - 1
    bottom = int(np.searchsorted(core_freqs, hi_used / span, side="right")) - 1
    if bottom < 0:
        # Downward room is exhausted — the WOOFER case, where a low Fc slides
        # ``hi_used`` down to meet a raised trusted floor. Make the deficit up
        # UPWARD from the core's own bottom bin, spending at most one floor width
        # of the driver's own low-pass skirt.
        bottom = 0
        top = int(np.searchsorted(core_freqs, core_freqs[0] * span, side="left"))
        if top >= core_freqs.size:
            # Neither direction has room: the core mask is itself narrower than
            # the floor, so take the whole-mask fallback — which for a core this
            # small is what the widening was converging on anyway.
            return core

    widened = np.zeros_like(core)
    widened[core_idx[bottom:top + 1]] = True
    return widened


def _target_and_plateau_db(
    smoothed_db: np.ndarray, level_mask: np.ndarray,
) -> tuple[float, float]:
    """``(target_level_db, plateau_level_db)`` — the design doc's target-level
    rule (median, NOT the band minimum) plus the normalization-budget plateau
    (the SAME region's own maximum).
    """
    band = smoothed_db[level_mask]
    return float(np.median(band)), float(np.max(band))


def driver_core_level_db(
    primary: DriverResponse, envelope: EnvelopeCurve,
    *, radiating_band_hz: tuple[float, float] | None = None,
) -> float | None:
    """One driver's own PASSBAND level — the starting estimate, not the level fact.

    Runs :func:`fit_driver_linearization`'s own resample -> ladder-smooth ->
    core-mask -> median chain, and exists as a separate entry point because it
    is read across ALL drivers before any one of them is fitted.

    **Demoted and kept** (ruling S8). "Level-matched" means matched acoustic
    output through the HANDOVER REGION, measured by ``solve_branch_trims``'
    power mean over the mirrored +/-1-octave halves about Fc. Passband-average
    sensitivity is a different quantity — the starting estimate that sizes a
    horn's fixed attenuation — and on a sloped horn the two legitimately differ
    by many dB, so their gap is disclosed and never reconciled. It does not
    place the trim pair, and since #2609 nothing derived from it does.

    ``radiating_band_hz`` (#1929) narrows the median to where this driver's own
    crossover leaves it radiating; the composer solves it, so nothing here knows
    what a crossover is. ``None`` is the pre-#1929 whole-core-mask median byte
    for byte. The narrowing is subject to :func:`_core_level_mask`'s width
    floor, and :func:`core_level_band_hz` reports which way that went.

    **Why the band matters, and only here.** The core mask is bounded by the
    driver's declared ``measurement_band_hz`` — a capture-COVERAGE declaration
    that routinely reaches past Fc — and a MEDIAN is a rank statistic, so a
    −40 dB stopband bin counts as much as a passband one. On the 2026-07-30 JTS3
    session a woofer declared to 4000 Hz against a 2000 Hz LR4 put ~28 % of its
    core bins inside its own stopband and read 3.4 dB away from the trim solve.
    The contamination is specific to the rank statistic, so the fix is: the
    give-back is a POWER-domain average and already effectively immune, and
    ``target_level_db`` is a different question with a different right answer.

    Returns ``None`` — not a number — when the envelope allows correction
    nowhere. That driver's level is UNKNOWN, and a placeholder would let one
    unmeasurable driver move every other one.
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
    """The span :func:`driver_core_level_db` ACTUALLY reads its median over, for
    the same arguments — ``None`` when it would return ``None``.

    Exists so a caller that discloses the band discloses the realized one rather
    than the bound it asked for. The divergence that MATTERS is
    :func:`_core_level_mask`'s width floor refusing the bound. The two are not
    otherwise equal: this band is resolved onto the envelope's own grid, so a
    snap of up to one bin is ordinary rather than a signal. Read a difference
    wider than a bin as the floor firing, a sub-bin one as quantization.
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
    """Adaptive fit-band trim. Returns inclusive ``(lo_idx, hi_idx)`` grid indices.

    The seed is CURVE-SHAPE-DRIVEN, not trust-driven: the extremes of
    ``envelope_mask`` bins whose smoothed value is already within one cut budget
    of ``target_level_db``. Deliberately NOT the mic-trust/class-prior "core"
    region — a driver's natural acoustic rolloff toward its crossover has
    nothing to do with mic trust, and for a woofer band entirely below its
    taper breakpoints the core spans the WHOLE eligible range, so seeding from
    its extremes would start the walk at the outer edge with no rolloff left to
    trim.

    From that seed it extends outward toward each edge of ``envelope_mask``,
    stopping the FIRST time the smoothed curve drops below the floor or the mask
    itself ends (so a non-contiguous mask is handled safely).
    """
    idxs = np.flatnonzero(envelope_mask)
    floor_db = target_level_db - PER_FILTER_CUT_CAP_DB
    within_budget = envelope_mask & (smoothed_db >= floor_db)
    seed_idxs = np.flatnonzero(within_budget)
    if seed_idxs.size:
        seed_lo, seed_hi = int(seed_idxs[0]), int(seed_idxs[-1])
    else:
        # Degenerate: no bin anywhere is within budget of target. Seed from the
        # single closest bin so the walk still has somewhere to start; both loops
        # then stop immediately, collapsing to a 1-bin band rather than crashing.
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
    ``design_peq`` is greedy over the residue, so a bin reading "you are 52.9 dB
    too loud" wins the search whatever the target says.

    The band has to be WIDENED because masking at the -3 dB edge refuses cuts
    this repo has twice measured as genuine work — a resonance 0.17 octaves past
    the edge, and a tweeter bump 0.39 octaves inside its high-pass edge. The
    margin is the SAME half octave ``BranchTarget.gain_band_hz`` widens by,
    imported rather than restated, and it transfers because its derivation does:
    half an octave past an LR4 edge the branch still moves the sum by 0.285 dB
    per dB of its own change, and that leverage argument is direction-agnostic.

    ``None`` narrows nothing, and so does an EMPTY intersection — same fallback
    posture, and the same reason, as :func:`_core_level_mask`.
    """
    if radiating_band_hz is None:
        return band_mask
    lo_hz, hi_hz = radiating_band_hz
    narrowed = band_mask & (
        (grid_hz >= octave_scaled(lo_hz, -STOPBAND_GAIN_MARGIN_OCTAVES))
        & (grid_hz <= octave_scaled(hi_hz, STOPBAND_GAIN_MARGIN_OCTAVES))
    )
    return narrowed if narrowed.any() else band_mask


# A falling top octave is compensated by ``_hf_continuation_stage`` below, which
# corners a Lowshelf near the deficit's ONSET and works in the cut domain, not
# by a falling-slope Lowshelf cornered at the fit band's low edge. ``_shelf_stage``
# stays the RISING-slope Highshelf; the two are mutually exclusive.
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
    """Fit ONE cut-only Highshelf if the fit band's smoothed slope rises faster
    than :data:`SHELF_SLOPE_THRESHOLD_DB_PER_OCT`. ``None`` when no shelf is
    warranted (falling or shallow slope, too few points to regress, or no
    normalization budget left).

    ``shape_db`` (R10a, #1817) is the branch's re-centred crossover shape, and
    the regression runs on ``smoothed_db - shape_db`` — the branch's OWN slope.
    Without it the gate is armed by the crossover alone: on a flat tweeter
    behind a 2 kHz LR4 the raw band slope is +5.6957 dB/oct against a
    shape-removed 0.0000. ``None`` regresses the raw curve, as before R10a.
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

    # Normalization-budget clamp: what is left of the spend budget once the
    # plain target-vs-plateau gap is accounted for.
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

    ``filters`` is empty on every non-firing path. When the stage FIRES it is
    ``(lowshelf, *peaking_cuts, taper?)`` in the emitter's shelf-first /
    taper-last order — the caller inserts ``filters[0]`` at position 0 and
    appends the rest. ``suppressed_reason`` is the sole non-empty field on an
    objective-gate suppression.
    """

    filters: tuple[LinearizationFilter, ...]
    spend_db: float
    ceiling_hz: float
    policy: str
    suppressed_reason: str
    measured_deficit_at_ceiling_db: float


# The empty/zeroed "did not fire, no objective suppression" result. Shared so an
# ineligible driver and a nothing-to-do skip cannot drift apart.
_HF_INERT = _HfContinuation(
    filters=(), spend_db=0.0, ceiling_hz=0.0, policy="",
    suppressed_reason="", measured_deficit_at_ceiling_db=0.0,
)


def _hf_suppressed(reason: str) -> _HfContinuation:
    """An objective-gate suppression: no filters, only ``suppressed_reason`` set.
    Suppression is visible and named, never a silent no-op."""
    return _HfContinuation(
        filters=(), spend_db=0.0, ceiling_hz=0.0, policy="",
        suppressed_reason=reason, measured_deficit_at_ceiling_db=0.0,
    )


def _hf_confidence_ceiling_and_knee_hz(
    grid_hz: np.ndarray, mic_trust_term: np.ndarray,
) -> tuple[float, float]:
    """``(ceiling_hz, knee_hz)`` from the mic-trust term's own taper.

    ``ceiling_hz`` is the first bin where mic-trust reaches ~0 — the frequency
    above which the calibrated mic resolves nothing — with ``grid_hz[-1]`` as
    the separate fallback for a term that never reaches 0. On today's reference
    tier those read the same number for different reasons. ``knee_hz`` is the
    first bin BELOW the ceiling sentinel; the test is deliberately the same one
    :func:`_core_or_fallback_mask` uses, so "still fully trusted" means one
    thing across this module.
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
    """The repeat-agreement gate: ``""`` when the repeats agree well enough over
    the compensation band, else a suppression reason.

    Spread is the per-bin ``max - min`` across ALL of the capture's sweep
    occurrences — the PRIMARY plus its repeats, matching ``compute_sigma_curve``'s
    own occurrence set. The primary MUST be in it: the fit is sized from the
    primary, so a primary carrying an outlier its repeats do not reproduce has
    to be caught here or the stage sizes a several-dB-too-hot lift from one bad
    sweep. Fewer than :data:`_HF_MIN_OCCURRENCES` occurrences is no
    reproducibility evidence and suppresses.
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
    """The CD-horn compensation stage (#1668): measured-inverse top-octave lift,
    realized cut-only via give-back. Runs AFTER the peaking loop.

    The tweeter-on-a-horn measures a real, EQ-able falling top octave. This
    stage sizes a lift from that MEASURED deficit (class-blind), realizes it in
    the CUT domain — cut everything below the compensation region by ``spend``
    so the flow's trim re-solve levels the branches back and the top octave
    lands ``spend`` dB higher RELATIVELY — and, only above the confidence
    ceiling where nothing is measurable, applies a declared-class continuation
    policy. Objective gates suppress it rather than ship a guess.

    Every filter it can emit is a cut: the cut-only invariant binds here too.
    """
    mic_trust = envelope.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    ceiling_hz, knee_hz = _hf_confidence_ceiling_and_knee_hz(grid_hz, mic_trust)

    # -- Applicability (not-applicable → silent inert result) --------------
    # The fit band must reach the confidence-ceiling region: a woofer or mid
    # topping out below the mic knee has no top-octave deficit to compensate,
    # which is what keeps the stage role-agnostic without a per-role branch. A
    # rising-slope Highshelf already emitted means the stage does not apply
    # (mutual exclusivity). Both are "not this driver" — inert, no reason.
    if fit_hi_hz < knee_hz:
        return _HF_INERT
    if any(f.biquad_type == "Highshelf" for f in filters):
        return _HF_INERT
    # The stage APPLIES but the flattening loop already spent every slot — a
    # named suppression, not a silent inert, because an eligible lift was dropped.
    if len(filters) >= MAX_FILTERS_PER_DRIVER:
        return _hf_suppressed("no_filter_budget")

    # -- Agreement gate (objective suppression) ----------------------------
    disagreement = _hf_repeat_spread_ok(grid_hz, primary, ceiling_hz)
    if disagreement:
        return _hf_suppressed(disagreement)

    ceiling_idx = int(np.argmin(np.abs(grid_hz - ceiling_hz)))

    # -- Desired compensation C(f) (the measured inverse) ------------------
    # Against the target CURVE since R10a (#1817). On the branch this stage
    # fires for the crossover shape is ~0 dB in the compensation region, so this
    # is near-identical to the old scalar in practice. It reads the curve anyway
    # because the ONSET walk runs down toward ``trusted_mid_hz``, and a scalar
    # target would let a branch's own rolloff open that walk early.
    deficit_db = target_curve_db - working_db
    measured_deficit_at_ceiling_db = float(max(0.0, deficit_db[ceiling_idx]))

    # Onset: the first bin ABOVE the trusted band's geometric midpoint where the
    # smoothed deficit rises through _MIN_FILTER_GAIN_DB AND stays positive all
    # the way to the ceiling — a contiguous, real falling region rather than a
    # lone blip in the otherwise-flat trusted band.
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
    # realize on a real curve (the binding one in practice today).
    spend = min(
        measured_deficit_at_ceiling_db,
        remaining_budget_db,
        HF_SINGLE_SHELF_SPEND_CAP_DB,
    )
    if spend < _MIN_FILTER_GAIN_DB:
        # Nothing meaningful to give back — an honest no-op, not an objective
        # suppression.
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
    # Biquad cascades commute acoustically, so inserting the backbone at filter
    # position 0 (the emitter's shelf-before-peaks contract) is order-safe.
    #
    # The shelf's own gain is CLAMPED at PER_FILTER_CUT_CAP_DB — a hard invariant
    # on every emitted filter, independent of the larger total spend budget. Past
    # the cap the shelf carries the first 12 dB and the peaking residual absorbs
    # the remainder.
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

    # Fit the residual (cut_target - lowshelf) with peaking cuts, capped per-bin
    # exactly as the flattening loop does. The cuts land in the TRUSTED band; the
    # top octave itself gets NO peaking filter — its lift arrives via the
    # give-back.
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

    # Fit-quality check: the realized cut cascade must track cut_target across
    # [onset, ceiling] to within HF_REALIZATION_TOLERANCE_DB, or the whole stage
    # is suppressed rather than ship a mis-shaped lift.
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
    if (
        policy == "taper"
        and len(filters) + len(emitted) < MAX_FILTERS_PER_DRIVER
        and ceiling_hz < _HF_TAPER_NYQUIST_HZ
    ):
        # One trailing Highshelf CUT above the ceiling walks the relative lift
        # back DOWN across the band we cannot see. Appended LAST (the emitter's
        # taper-last construction contract).
        #
        # The corner is CLAMPED, never skipped, at the geometric mean of two
        # bounds: ABOVE ceiling_hz, or the whole transition sits inside the band
        # whose realization was already graded without this filter; and BELOW
        # Nyquist, where ``_validated_biquad_entry`` refuses the config and an
        # RBJ shelf degenerates to a literal pass-through. min() therefore
        # engages a little before the designed corner would become illegal,
        # which is deliberate — clamping only once illegal parks the corner
        # arbitrarily close to Nyquist and hits that degeneracy.
        taper_hz = min(
            ceiling_hz * _HF_TAPER_CORNER_RATIO,
            math.sqrt(ceiling_hz * _HF_TAPER_NYQUIST_HZ),
        )
        taper_gain = -min(spend / 2.0, HF_TAPER_MAX_DB)
        if -taper_gain >= _MIN_FILTER_GAIN_DB:
            emitted.append(LinearizationFilter(
                biquad_type="Highshelf", freq=taper_hz,
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
# without overshooting the desired lift. 24 halvings take a 12 dB range to well
# under a micro-dB, and the loop runs at most MAX_FILTERS_PER_DRIVER times.
_CUT_REDUCTION_BISECTION_STEPS: int = 24

# Materiality slack (dB) on the permitted-headroom test.
#
# NOT float noise: a biquad's response never reaches exactly 0 dB, so at 1e-6 a
# SINGLE far-field leakage bin whose permitted headroom is fractionally negative
# vetoes the entire shrink — measured, a -0.0034 dB bin delivered 0.00 of a
# wanted 4.00 dB. :data:`_ENVELOPE_NONZERO_EPS_DB` is this module's existing
# answer to the same question, and at 0.05 dB it sits ~15x above that leakage
# and 10x below the smallest gain this module emits, so it can mask neither a
# real overshoot nor a real filter.
_CUT_REDUCTION_EPS_DB: float = _ENVELOPE_NONZERO_EPS_DB


def reduce_cuts_for_lift(
    filters: Sequence[LinearizationFilter],
    wanted_db: np.ndarray,
    headroom_db: np.ndarray,
    grid_hz: np.ndarray,
) -> tuple[tuple[LinearizationFilter, ...], np.ndarray]:
    """Spend a desired lift by SHRINKING cuts we ourselves placed, before any
    boost is considered. Returns ``(filters, delivered_lift_db)``.

    A first-class operation, distinct from boost: two filters fighting each
    other cost a slot each, cost headroom the shrink would not have cost, and
    leave a phase response neither designed. Reducing a cut is free, uses no
    slot, and is exactly invertible.

    Two arrays, two jobs, deliberately not one:

    * ``wanted_db`` — how much lift would be USEFUL at each bin (``>= 0``).
      Drives which filters are worth touching and when to stop.
    * ``headroom_db`` — how much lift is PERMITTED at each bin. May be negative
      (any lift here is a regression) and may be ``+inf`` (nothing is claimed
      here). This is the SAFETY constraint, and it is what stops a cut placed to
      tame a peak from being unwound to fill an unrelated dip an octave away.

    Greedy, deepest cut first, by bisection; the delivered lift is computed with
    the real RBJ evaluator at both gains, never a linear-in-gain approximation,
    which is wrong by several tenths of a dB on a shelf and in the unsafe
    direction. A filter shrunk to within :data:`_MIN_FILTER_GAIN_DB` of unity is
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

    ``measured_headroom_db`` is ``target − measured``: positive is a measured
    deficit, zero or negative a measured excess where adding level makes the
    response worse.

    **The criterion is the filter's own half-gain bandwidth**, the same
    intrinsic, per-filter, scale-free action-region test
    :func:`_boost_exclusion_verdicts` uses — one filter read once against its
    own transfer function, no ordering, no search over subsets, no absolute dB
    threshold borrowed from a different geometry. A boost is dropped when
    ``measured_headroom_db <= 0`` at EVERY bin of its action region. A single
    bin of genuine deficit keeps it, which is deliberately the most permissive
    form: this catches a boost aimed entirely at a measured excess, not a boost
    with something real to fill.

    **Why MEASURED and not the working curve.** ``working`` is the measurement
    plus every cut placed above, and a cut's SKIRTS drag it down in a
    neighbourhood the cut was never aimed at — manufacturing a deficit the
    measurement does not have, which the lift stage then designs a boost to
    fill. Grading against ``working`` cannot see this by construction.

    The realization gate below grades against ``envelope.allowed_depth_db``,
    which across the core passband is the 24.0 dB CEILING SENTINEL — a "no limit
    expressed" marker — so its test there reads ``realized > 24.5``, a threshold
    carrying no information about what the measurement supports. That gate still
    binds wherever the envelope actually tapers; this adds the per-bin,
    directional evidence it never had.
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
            # this bound abstains. The stopband guard owns a cascade acting
            # entirely outside the band.
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
            # The LEAST-hot bin it acts on, stated as an excess. The honest
            # bound: it is the closest this filter came to having something to
            # fill, and it still had nothing.
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
    once against its OWN transfer function, so there is no ordering, no
    iteration and no "drop until it fits". That is what separates this from the
    arbitrary-ordering hazard ``interference_nulls.EXCLUSION_CAP_FRACTION``
    warns about.

    **The criterion is the filter's own half-gain bandwidth** — the standard
    parametric-EQ bandwidth convention — so the question is "does this filter's
    ACTION REGION overlap the band", which is scale-free: a +1 dB bell centred
    in the band goes; an +11.67 dB bell 0.7 octaves away delivering a sixth of
    its peak into the band is spill and stays.

    **Not an absolute threshold, in either direction.** Both were tried.
    :data:`~jasper.active_speaker.branch_target.SIGNIFICANT_GAIN_DB` is
    calibrated for the stopband guard, where any gain outside a widened passband
    is anomalous; an excluded band sits INSIDE the passband, so a whole-cascade
    test at that threshold refused the entire lift on 94.4 % of randomized
    multi-dip fits, destroying a median 14.83 dB of boost that lived almost
    entirely OUTSIDE the band. No threshold at all was infinitely permissive.

    Returns ``(kept, dropped, residual)``. ``residual`` carries the realized max
    still inside each band AFTER the drops — skirt tails by construction — and
    is disclosed rather than refused: the post-apply sweep measures what the
    speaker actually did.
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
    """The spans NO branch's own measured core band covers (#2599, #2600 item 4)
    — the gaps between the supplied :func:`core_level_band_hz` bands, ascending.

    A two-way speaker is measured one branch at a time and each core band stops
    where its own crossover hands off, so between the woofer's top and the
    tweeter's bottom NEITHER per-branch capture carries trusted evidence while
    the SUMMED response there is the phase-sensitive blend of both. On the
    2026-08-16 round-3 jts3 session that span was 1291.4104-2077.2412 Hz.
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

    **Run over the FINAL cascade, cuts and lift boosts alike.** Reading one
    stage's prescriptions instead let 74 hole-centred positive-gain boosts ship
    unnamed across a 400-fit randomized probe — the worst class to miss, because
    a boost adds level into the phase-sensitive blend on evidence no branch has.
    Reading the emitted list makes the universal true by construction.

    **What the disclosure is for.** A filter centred inside a hole is one branch
    acting alone where the per-branch instrument is silent for EVERY branch. On
    the 2026-08-16 jts3 run a -1.7577 dB woofer cut at 1404.4032 Hz landed on the
    blend dip and deepened it, and nothing in the receipt said a filter had been
    placed where no measurement reached.

    **Why it REPORTS instead of refusing.** A refusal needs a rule separating
    this cut from legitimate work and no band criterion does: refusing every
    centre inside the conductor fixture's hole drops a -7.821 dB cut sitting on
    +7.821 dB of measured excess and turns that fixture's PASSING correction
    into ``not_an_improvement``. Clamping to each branch's own core band
    additionally reverses #1809's measured ruling that a cut past the handoff is
    ordinary useful work. The one thing that WOULD separate the populations is
    magnitude, and inventing a dB threshold in that gap is the "constant
    borrowed across two geometries" bug :func:`_boost_exclusion_verdicts`
    documents. The honest separator needs the SUM, which only the
    alignment/crossover layer sees.

    **Peaking only.** A shelf's ``freq`` is a CORNER, not a placement: its
    authority is the whole band to one side of it, so asking whether that single
    frequency sits inside a hole is a category error. Skipped by type, not by
    stage — every Peaking filter from every stage is read.
    """
    if not blind_bands_hz:
        return ()
    placements: list[BlindZonePlacement] = []
    for emitted in filters:
        # Peaking only — a shelf's ``freq`` is a CORNER, not a placement.
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

    Runs LAST, on whatever deficit survives the shelf, peaking and CD-horn
    stages: ``target_curve_db − working_db``, clipped at zero, inside the fit
    band. Two moves, in this order: :func:`reduce_cuts_for_lift` (free, no slot,
    no headroom), then boost filters for the residue if the vocabulary allows.

    **Null exclusion still binds**: the desired lift is clamped per bin by
    ``envelope.allowed_depth_db``, which is already zero wherever the
    interference-null registry or the position screen excluded a band. Nothing
    here re-derives that judgement.

    **Inert under a cut-only vocabulary**, and not as a formality: a cuts-only
    loop leaves the whole curve at or BELOW target, so every dip is a "deficit"
    by this stage's arithmetic, and chasing it would silently change what every
    pre-PR-L5 caller gets.

    ``contribution`` (R10a, #1968) scales the WANTED deficit by the branch's
    output as a fraction of its own full output, so a deficit where the
    crossover has taken this branch mostly out of the sum attracts
    proportionally less boost. Gain side only — see
    :mod:`jasper.active_speaker.branch_target` for why cuts are not weighted.

    **The stopband-gain guard (#1968) is structural**, and the one thing here
    that can refuse a cascade the old code emitted: a realized boost cascade
    putting more than :data:`SIGNIFICANT_GAIN_DB` outside ``gain_permitted`` is
    refused as ``"stopband_gain"``. A REALIZED-response check and not a mask on
    the request, because a bell has SKIRTS — a boost centred just inside the
    radiating edge puts real gain a half-octave past it. Read over the WHOLE
    grid and not ``band_mask``, because the mask's overlap with the stopband is
    incidental rather than guaranteed: on the 2026-07-30 JTS3 session a
    mask-limited guard would have seen 7 of the woofer's 78 stopband bins and
    NONE of the tweeter's 89, in the same session.
    ``test_a_mask_limited_guard_would_miss_these_bins_entirely`` pins it.

    **The measured-target bound (#2599)** runs on the designed boosts, per
    filter, against ``target_curve_db − measured_db`` — the branch's own
    measurement, NOT the post-cut ``working_db`` the request is derived from.
    See :func:`_boost_evidence_verdicts`.

    **The boost-evidence bound (#1967)** runs last, per filter, on the emitted
    response, dropping any boost whose action region overlaps a contradicted
    band and disclosing the remaining skirt
    (:func:`_boost_exclusion_verdicts`). Only when EVERY boost is dropped does
    the lift come back empty.

    Suppressed (named, never silent) when no filter slots remain, when
    ``design_peq`` cannot realize the residue, when the realized cascade
    overshoots the envelope's allowance or puts gain in the stopband, or when
    every boost designed was refused by one of the two bounds.
    """
    if not vocabulary.allow_boost:
        return _Lift(tuple(filters), 0.0, 0.0, 0.0, "")

    # How much lift is PERMITTED per bin: the distance to target inside the fit
    # band (negative where the curve already sits above it — a cut there may not
    # be unwound), and unconstrained outside it. Read over the whole fit band,
    # NOT ``lift_mask``: a bin the crossover has handed off is still a bin a
    # lifting filter's skirt must not overshoot into.
    headroom_db = np.where(band_mask, target_curve_db - working_db, np.inf)
    # How much lift is WANTED: the positive part of the same distance, bounded
    # per bin by the envelope's allowance, which is direction-agnostic and
    # already zero wherever a band was excluded. ``lift_mask`` narrows WANTED to
    # the driver's own radiating band (#1809), which is what makes the bound
    # boost-only; it defaults to the fit band, so a caller with no crossover to
    # declare gets the pre-#1809 stage exactly.
    wanted_mask = band_mask if lift_mask is None else lift_mask
    deficit_db = np.clip(
        np.where(wanted_mask, target_curve_db - working_db, 0.0), 0.0, None,
    )
    if contribution is not None:
        # #1968's contribution weighting, gain side only. Scales what the stage
        # ASKS FOR, never what it is allowed to spend.
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
    # never be placed where this driver has handed off.
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
    # per-bin allowance. A greedy bell fit can overshoot between its centres.
    realized_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(tuple(boosts), grid_hz)), 1e-12)
    )
    allowance = np.maximum(envelope.allowed_depth_db, 0.0)
    if np.any(realized_db[band_mask] > allowance[band_mask] + _MIN_FILTER_GAIN_DB):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "exceeds_envelope",
        )

    # #1968's hard rule, enforced on the cascade that will actually be emitted:
    # no significant gain more than half an octave past this branch's acoustic
    # passband edge. Read over the WHOLE grid, NOT ``band_mask`` — see this
    # function's docstring for the measured coverage gap that causes.
    if gain_permitted is not None and np.any(
        realized_db[~gain_permitted] > SIGNIFICANT_GAIN_DB
    ):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "stopband_gain",
        )

    # #2599's measured-target bound. Per filter, drop-only, on the cascade that
    # will actually be emitted, placed AFTER both whole-cascade gates above so a
    # cascade they refused cannot come back as an accepted subset. The evidence
    # is the branch's own measurement, and the question is the opposite
    # direction: not "was boosting here contradicted" but "is there anything
    # here to boost".
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
    # reason: on the cascade that will actually be emitted, over the whole grid.
    #
    # **Why not a mask on the request.** Zeroing ``wanted`` inside these bands is
    # wrong twice, both measured on this stage. It confines bell CENTRES and not
    # their SKIRTS, so design_peq places filters at the band edges and the skirts
    # refill it (3.66 dB removed, +9.93 dB left inside). And it is NOT MONOTONE:
    # this stage's suppressions are all-or-nothing and design_peq is greedy over
    # the residue, so REMOVING demand can unlock a cascade that was refused
    # wholesale — one measured case went from no boost at all to a +24.06 dB
    # cascade carrying +12.85 dB MORE gain inside the excluded band.
    #
    # Reading the realized cascade has neither problem: the cascade designed here
    # is bit-identical with and without this bound, and dropping filters can
    # never raise the gain at any bin. Placed AFTER both gates above for the
    # same reason, and no re-spend or refit follows — bounded, deterministic,
    # nothing to oscillate.
    kept, dropped, residual = _boost_exclusion_verdicts(
        boosts, grid_hz, vocabulary.boost_excluded_bands_hz,
    )
    if dropped:
        if not kept:
            # Every boost was aimed at a contradicted band, so the lift is empty
            # and says why.
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
    # Whatever gain survives INSIDE an excluded band is now skirt tail, and it is
    # disclosed rather than refused: the post-apply sweep measures what the
    # speaker actually did.
    return _Lift(
        tuple([*reduced, *boosts]), requested_db, from_reduced_cuts_db,
        float(np.max(realized_db)), "",
        boost_excluded_drops=tuple(dropped),
        boost_excluded_residual=tuple(residual),
        boost_evidence_drops=tuple(evidence_drops),
    )


#: Every ``lift_suppressed_reason`` a fit can carry — pinned by a test so a new
#: suppression path cannot ship an un-enumerated reason string.
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
    """Fit one driver's linearization from its measured response and correction
    envelope.

    Cut-preferred, not cut-only: ``vocabulary`` decides whether a lift is
    admitted at all, and this function re-proves both that decision and the
    per-filter boost cap on its own output before returning. ``envelope``
    carries everything besides the raw magnitude curve — role, mic tier, driver
    class, repeat count, and the per-bin allowed correction depth.

    ``radiating_band_hz`` bounds two things at two widths, and the difference is
    the whole design. LIFT is bounded at the band ITSELF (#1809): the pathology
    is a driver spending GAIN against its own crossover — the 2026-07-28 JTS3
    woofer's +11.6155 dB at 2747 Hz arrived at +1.06 dB of net acoustic
    contribution and cost 11.6 dB of headroom. The SOLVE is bounded at that band
    widened by :data:`~jasper.active_speaker.branch_target.
    STOPBAND_GAIN_MARGIN_OCTAVES` (#2523), because a cut in the SHOULDER still
    reaches the sum and a cut 18 dB down the branch's own low-pass does not.
    ``None`` means unbounded, which is the honest answer for a one-way box.

    ``blind_bands_hz`` (#2599) is the cross-branch statement: spans NO branch's
    own measured core band covers. A peaking filter may not be CENTRED in one
    without being NAMED for it.

    ``target_level_db`` staying whole-region is a POSITIVE choice: it is the
    level every stage grades against, so it has to be derived from the bins the
    fit may place a filter on. ``driver_core_level_db`` is a different question
    — where does this driver SIT relative to its sibling — and since #1929 reads
    its median over the radiating band.

    ``target`` is that level's SHAPE (#1817) — a
    :class:`~jasper.active_speaker.branch_target.BranchTarget` carrying the
    branch's committed crossover magnitude, its contribution weight and the band
    a filter may put GAIN in — so no filter fights the crossover ANYWHERE rather
    than only outside the radiating band. The scalar did not change meaning: the
    shape is re-centred to add no level over the very band the scalar is the
    median of, so every level consumer reads the number it always did and only
    the per-bin GRADING moved. ``None`` is the flat target byte for byte.

    This fit is independent of the level datum, structurally: a driver is
    flattened to its OWN passband, and where that passband is PLACED relative to
    the others is a trim decided later and elsewhere.

    Algorithm:
      1. Resample ``primary``'s magnitude onto the envelope's grid, ladder-smooth.
      2. Fit band = envelope-nonzero bins, trimmed by the adaptive-band-trim
         walk, then narrowed to the SOLVE band. The LIFT band is that
         intersected with ``radiating_band_hz`` itself.
      3. Target level = median of the smoothed curve over the trusted core
         passband (NOT the band minimum).
      4. Shelf stage: one cut-only Highshelf on a rising slope, budget-clamped.
      5. Peaking loop: ``design_peq`` on the post-shelf residual, cuts-only,
         capped per-bin by ``min(PER_FILTER_CUT_CAP_DB, allowed_depth_db)``.
      6. CD-horn compensation stage: measured-inverse top-octave lift realized
         cut-only via give-back, gated by repeat agreement and realization
         fit-quality. When it fires the claims below are computed in the
         give-back frame (``target_level_db - spend``).
      7. Lift stage: the surviving deficit inside the lift band, spent first by
         SHRINKING this fit's own cuts and then, if the vocabulary allows, by
         boost filters — envelope-bounded per bin and measured-target bounded
         per filter.
      8. Blind-zone disclosure: every EMITTED Peaking filter centred inside a
         ``blind_bands_hz`` span is named. Reports only.

    Returns a fit with zero filters (an honest no-op) when the envelope allows
    correction nowhere.
    """
    grid_hz = envelope.freqs_hz
    measured_db = np.interp(grid_hz, primary.freqs_hz, primary.magnitude_db)
    smoothed_db = _ladder_smooth(grid_hz, measured_db)

    envelope_mask = envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB
    if not envelope_mask.any():
        return _empty_fit(envelope)

    level_mask = _core_or_fallback_mask(envelope, envelope_mask)
    target_level_db, plateau_level_db = _target_and_plateau_db(smoothed_db, level_mask)

    # The target's SHAPE (#1817). Re-centred on the SAME mask the scalar above is
    # the median of, so it adds shape without moving level. ``None`` leaves
    # ``target_curve_db`` a flat array at the scalar.
    #
    # The grid check is explicit and loud because the failure it replaces is
    # neither: a target built on the DRIVER RESPONSE's grid rather than the
    # ENVELOPE's surfaced as an IndexError several frames down. Same
    # explicit-raise posture as the invariants below — this is hardware-bound
    # output, and `assert` is stripped under `python -O`.
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
    # Since R10a the fit grades against the branch's own IDEAL digital crossover,
    # and a real branch does not follow one into its deep stopband: it flattens
    # into breakup, leakage and the noise floor, and every dB of that gap arrived
    # at the solve as a demand. Reconstructed in
    # ``test_out_of_band_content_does_not_reach_the_solve``: the pre-#2523 solve
    # spent ALL EIGHT slots between 9.7 and 11.8 kHz on a branch declared to
    # radiate only to 1282.3 Hz, and the budget that would have corrected the
    # CORE band went out of band with them.
    #
    # **The bound is on what the solver is FED, never on how its output is
    # judged.** Every honesty guard downstream is untouched. It deliberately does
    # NOT narrow ``level_mask`` (#1929's question keeps #1929's answer) or
    # ``_observe_octave_summary``, which is the DISCLOSURE layer: a stopband the
    # fit no longer solves in is still one a reader is entitled to see.
    band_mask = _solve_band_mask(grid_hz, band_mask, radiating_band_hz)
    # Re-read the band's edges off the mask the stages will actually run on, so
    # design_peq's band, the shelf's regression, the CD-horn eligibility, the
    # VERIFY band and the reported ``fit_band_hz`` cannot disagree with it. A
    # no-op narrowing reproduces the trim's own indices exactly.
    solve_idx = np.flatnonzero(band_mask)
    fit_lo_idx, fit_hi_idx = int(solve_idx[0]), int(solve_idx[-1])

    # Where LIFT may go (#1809) — the fit band clamped to the side of the
    # crossover this driver actually radiates on.
    #
    # **Boosts only, and the asymmetry is the whole design.** A CUT outside the
    # radiating band is ordinary useful work: the crossover has attenuated the
    # band but not silenced it, so the cut still moves the sum, and it is free of
    # the two things that make the boost case a defect — it spends no headroom (a
    # cut cannot raise a peak) and it cannot fight the crossover, because past
    # the edge the curve is already BELOW target. A BOOST there is attenuated by
    # the same crossover it fights and charges full headroom for that nothing.
    #
    # Inside the band the fit still lifts the last few dB before the edge (a flat
    # driver behind an LR4 attracts +2.379 dB at 0.79*Fc); that is bounded by the
    # edge attenuation itself and is cheap. The real fix is the crossover-shaped
    # TARGET (#1817), not a wider bound.
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
        # THE #1817 SITE. This array was a flat line, which is what made a branch
        # measured THROUGH its own crossover read that crossover's rolloff as a
        # driver deficit. It is now the branch's own crossover shape at the same
        # level.
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

    # CD-horn compensation stage (#1668). Runs AFTER the peaking loop so its
    # deficit is measured against the post-flattening working curve.
    hf = _hf_continuation_stage(
        grid_hz, working_db, target_curve_db, target_level_db, plateau_level_db,
        envelope, primary, fit_lo_hz, fit_hi_hz, filters,
    )
    if hf.filters:
        # The Lowshelf backbone goes to position 0 (the emitter's
        # shelf-before-peaks contract); the rest are appended.
        filters = [hf.filters[0], *filters, *hf.filters[1:]]
        working_db = working_db + 20.0 * np.log10(
            np.maximum(np.abs(complex_correction_response(hf.filters, grid_hz)), 1e-12)
        )

    # Lift stage (PR-L5): reduce our own cuts, then boost the residue. Runs LAST
    # so its deficit is measured against everything the cut-only stages achieved
    # — and, when the CD-horn stage fired, in that stage's give-back frame, which
    # is the level the branch will actually be trimmed back to.
    lift = _lift_stage(
        grid_hz, working_db, target_curve_db - hf.spend_db, envelope,
        band_mask, filters, vocabulary,
        # The MEASUREMENT, not the working curve — #2599's bound exists precisely
        # because the two disagree once cuts are placed.
        measured_db=smoothed_db,
        lift_mask=lift_mask,
        contribution=None if centred_target is None else centred_target.contribution,
        gain_permitted=(
            None if centred_target is None else centred_target.gain_permitted
        ),
    )
    filters = list(lift.filters)

    # Restore the emitter's taper-last contract. ``_lift_stage`` appends its
    # boosts to the list it was handed, so a trailing CD-horn taper stops being
    # trailing whenever a boost survives. Reordering is acoustically free but not
    # structurally: the emitter classifies the taper slot BY POSITION and refuses
    # a shelf that lands anywhere else. A fit carries at most one Highshelf, so a
    # Highshelf past index 0 is that taper.
    taper_at = next(
        (i for i, f in enumerate(filters) if i and f.biquad_type == "Highshelf"),
        None,
    )
    if taper_at is not None and taper_at != len(filters) - 1:
        filters.append(filters.pop(taper_at))

    # THE #2599 PLACEMENT SITE. Every emitted Peaking filter whose centre lands
    # in a span NO branch's own capture covers is NAMED here; it still ships.
    #
    # Read AFTER the lift stage, on the FINAL list: reading the flattening loop's
    # prescriptions instead let 74 hole-centred lift BOOSTS ship unnamed across a
    # 400-fit probe. It also reports each cut's FINAL gain, after any shrink.
    #
    # Graded against ``target_curve_db``, NOT the give-back frame: this number
    # answers "how far above its own target does the measurement sit here".
    blind_zone_placements = _blind_zone_placements(
        filters, grid_hz, smoothed_db - target_curve_db, blind_bands_hz,
    )

    # THE CLAIM SEAM (R10b). Everything below this line is a REPORTED NUMBER —
    # residual, verify, observe, give-back — and every one is graded against the
    # cascade the graph will actually emit.
    #
    # Rebuilding from ``smoothed_db`` plus the WHOLE cascade rather than carrying
    # the incremental ``working_db`` forward does two jobs:
    #
    #  1. It cannot double-count. The lift stage can SHRINK a cut already folded
    #     in above, so an incremental update would apply that filter twice.
    #  2. It cannot grade an approximation. The peaking stage folds itself in
    #     with ``predicted_response``, whose bell is a Lorentzian in
    #     log-frequency; :func:`complex_correction_response` is the exact biquad.
    #
    # Job 2 is why this is unconditional: before R10b the rebuild was conditional
    # on the lift stage changing the list, so a cut-only vocabulary reported
    # residuals computed against the Lorentzian — the fit grading itself with an
    # evaluator the hardware does not use.
    #
    # STAGE-INTERNAL arithmetic is deliberately left alone: a search heuristic
    # picking its next peak off an approximate residual is a fit-quality
    # question, a CLAIM is a correctness one. This rebuild is the last write to
    # ``working_db`` and no filter-producing stage runs after it, so it cannot
    # move a single emitted filter on any path.
    #
    # WHAT THIS SEAM DOES NOT REACH (#2013): it makes the claim CURVE exact, not
    # the claim FRAME. ``frame_target_db`` below carries ``hf.spend_db``, sized
    # ABOVE this line against the Lorentzian-folded ``working_db`` — worth
    # 0.143 dB of spend and up to 0.162 dB of committed trim on the banked
    # 2026-07-30 session, which is larger than anything this seam itself moves.
    # The suppression verdict did not flip on any of the 8 rows: "not shown to
    # flip on this corpus", not "shown safe".
    working_db = smoothed_db + 20.0 * np.log10(
        np.maximum(
            np.abs(complex_correction_response(tuple(filters), grid_hz)), 1e-12
        )
    )

    # An explicit raise, not a bare `assert`: this is a safety invariant on
    # HARDWARE-BOUND output and `assert` is stripped under `python -O`. PR-L5
    # made the invariant conditional on the VOCABULARY rather than
    # unconditional; it did not weaken it, since a cut-only vocabulary is held to
    # exactly the same raise as before.
    if not vocabulary.allow_boost and any(f.gain > 0.0 for f in filters):
        raise RuntimeError("linearization fit emitted a boost under a cut-only vocabulary")

    # Per-filter caps are HARD invariants on every emitted filter, and the total
    # spend/boost can legitimately exceed them, so re-prove them here rather than
    # trusting each stage's own clamp.
    if any(f.gain < -PER_FILTER_CUT_CAP_DB - 1e-6 for f in filters):
        raise RuntimeError("linearization fit exceeded the per-filter cut cap")
    if any(
        f.gain > vocabulary.per_filter_boost_cap_db + 1e-6 for f in filters
    ):
        raise RuntimeError("linearization fit exceeded the per-filter boost cap")

    # The give-back this driver's correction actually removed from its own
    # reference (core) band, published as ``core_band_giveback_db``. It does not
    # anchor a trim; the anchor measures its own give-back over
    # ``branch_level_bands_hz``.
    #
    # The MEASURED before-vs-after core-band level delta. Averaging the
    # CORRECTION alone instead would be power-domain approximate — exact only for
    # a flat core band, and up to ~1.1 dB under-return on a 12 dB-tilted one —
    # because a power-domain mean of the correction cannot know which bins carry
    # the level it is being subtracted from.
    correction_giveback_db = 0.0
    if filters:
        correction_giveback_db = (
            _power_band_average_db(smoothed_db, level_mask)
            - _power_band_average_db(working_db, level_mask)
        )

    # Give-back frame: when the CD-horn stage fired it cut the whole band by
    # ``spend`` so the trim re-solve levels the branches back, making the honest
    # reference for the claims below the target curve MINUS spend.
    # ``hf.spend_db`` is 0 when the stage did not fire. The ``target_level_db``
    # FIELD still reports the original median. A CURVE since R10a: the give-back
    # rides on top of the crossover shape rather than replacing it.
    frame_target_db = target_curve_db - hf.spend_db
    residual = (working_db - frame_target_db)[band_mask]
    residual_rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    residual_max_db = float(np.max(np.abs(residual))) if residual.size else 0.0

    # Honesty-ladder levels 2/3 — computed over the SAME post-filter working_db
    # and give-back frame the FIT claim used, just wider bands.
    verify_band_hz, verify_residual_rms_db, verify_residual_max_db = (
        _verify_band_and_residual(
            grid_hz, working_db, frame_target_db, fit_lo_hz, fit_hi_hz,
        )
    )
    observe_octave_summary = _observe_octave_summary(
        grid_hz, working_db, frame_target_db,
    )

    # Octave centers ABOVE the confidence ceiling are disclosed as
    # beyond-measurement-confidence when the CD-horn stage fired: their relative
    # lift is a declared-class continuation, not a measured claim.
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
        # headroom_cost_db is deliberately left at its 0.0 default: the charge is
        # a property of the emitted branch chain, which this core does not know.
        lift_requested_db=lift.requested_db,
        lift_from_reduced_cuts_db=lift.from_reduced_cuts_db,
        lift_from_boost_db=lift.from_boost_db,
        lift_suppressed_reason=lift.suppressed_reason,
        lift_boost_excluded_drops=lift.boost_excluded_drops,
        lift_boost_excluded_residual=lift.boost_excluded_residual,
        lift_boost_evidence_drops=lift.boost_evidence_drops,
        blind_zone_placements=blind_zone_placements,
    )
