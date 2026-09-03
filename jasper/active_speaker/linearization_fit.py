# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Layer-1a driver-linearization fit engine (#1668 PR-C).

Consumes ONE driver's ``DriverResponse`` plus its ``EnvelopeCurve`` and
produces a cut-PREFERRED PEQ/shelf fit that flattens the driver toward a
per-session target level, honoring the envelope's per-bin correction-depth
ceiling. Pure computation: no I/O, no CamillaDSP emission. The allowed
vocabulary is an INPUT, not a hardcode (:class:`FitVocabulary`) — nothing
about the speaker's topology reaches this core. Lift is bounded to the
driver's radiating side of its crossover (#1809); the SOLVE runs over that
band widened by
:data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`
(#2523).

See docs/active-speaker-tuning-layers-design.md "Layer 1a concretely".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np

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

if TYPE_CHECKING:
    from jasper.audio_measurement.program_analysis import DriverResponse

# --------------------------------------------------------------------------- #
# fitting policy constants
# --------------------------------------------------------------------------- #

# Per-filter cut ceiling, dB. Shared by the shelf stage and the peaking loop's
# per-bin cap array — design doc "cuts generous (-12 dB, Q<=8)".
PER_FILTER_CUT_CAP_DB: float = 12.0

# Bound on TOTAL normalization spend across the whole fit — how far below the
# driver's own core-passband peak the fit may settle. 18 dB covers the
# measured JTS3 tweeter deficit (14.2-14.3 dB at the reference-tier
# confidence ceiling) with margin. A max-SPL LEDGER cost, disclosed via
# ``correction_giveback_db``/``hf_continuation_spend_db``. Can exceed
# PER_FILTER_CUT_CAP_DB (no single filter may) — a stage's spend is clamped
# to the REMAINING budget. ``target_level_db`` itself is left UNCLAMPED.
MAX_NORMALIZATION_SPEND_DB: float = 18.0

# Slope (dB/octave over log2(f)) above which the fit band is a genuine
# tilted shelf rather than local ripple. Only a RISING slope fires the shelf
# stage; a falling one is left to the peaking loop (cut-only cannot correct
# a naturally falling response).
SHELF_SLOPE_THRESHOLD_DB_PER_OCT: float = 3.0

# Hard cap on filters per driver (shelf + peaking) — design doc "Fitting
# policy".
MAX_FILTERS_PER_DRIVER: int = 8

# Per-filter BOOST ceiling, dB (PR-L5) — deliberately equal to
# PER_FILTER_CUT_CAP_DB. A REALIZATION bound, not a policy cap (survives the
# owner's "arbitrary gain caps GO" ruling): TOTAL boost stays unbounded
# because a cascade composes.
PER_FILTER_BOOST_CAP_DB: float = 12.0

# A bin below this allowed-depth is "the envelope permits nothing here"
# (float noise or a taper's asymptotic tail).
_ENVELOPE_NONZERO_EPS_DB: float = 0.05

# Below this magnitude a filter is cosmetic — mirrors design_peq's own
# default ``min_filter_gain_db``, kept LOCAL since it also gates the shelf
# stage's worth-adding check.
_MIN_FILTER_GAIN_DB: float = 0.5

_PEAKING_Q_MAX: float = 8.0

# Narrowest a BOOST bell may be. Passed EXPLICITLY at the lift stage's
# ``design_peq`` call — load-bearing for the #1967 drop-radius safety
# property (Q 1.0 -> +/-0.68 octaves, Q 0.5 -> +/-1.25, Q 0.3 -> +/-1.85).
_PEAKING_Q_MIN: float = 1.0
_PEAKING_FLATNESS_TARGET_DB: float = 1.0

# The RBJ Highshelf's fixed Butterworth Q, from
# ``camilla_config_contract.SHELF_Q`` — the APPLY stage spells this same
# number into the emitted CamillaDSP ``q``, so this module's model matches
# what the speaker realizes. Keep in lockstep;
# ``tests/test_sound_peq_response.py`` pins CamillaDSP's slope<->Q formula.
# At ``slope: 6`` the realized Q collapses to 0.476 at -11 dB (the
# 2026-07-27 shelf-Q defect).

# Octave-band centers for the candidate artifact's compact reason summary.
_OCTAVE_BAND_CENTERS_HZ: tuple[float, ...] = (
    250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 12000.0, 16000.0, 20000.0,
)

# --------------------------------------------------------------------------- #
# CD-horn compensation stage constants (#1668)
# --------------------------------------------------------------------------- #

# Low bound of the CD-horn continuation stage's compensation/agreement band —
# the deficit is expressed relative to the driver's trusted 4-8 kHz band.
HF_COMPENSATION_BAND_LO_HZ: float = 4_000.0

# Repeat-agreement gate (objective suppression). Per-bin spread (max-min
# across repeat sweeps, ladder-smoothed) over the compensation band must
# stay under these limits or the stage is suppressed. Two tiers because
# measurement noise grows with frequency; sourced from the owner's
# per-serial UMIK-2 uncertainty research (stock-cal ~+/-1.5 dB @12 kHz /
# +/-2.3 dB @16 kHz).
HF_AGREEMENT_LIMIT_LOW_DB: float = 1.0
HF_AGREEMENT_LIMIT_HIGH_DB: float = 2.0

# Frequency (Hz) splitting the two agreement tiers above.
_HF_AGREEMENT_TIER_SPLIT_HZ: float = 10_000.0

# Minimum sweep occurrences (primary + repeats) the agreement gate needs —
# the N>=3 "paired gate" (sigma-seeding report finding 5). LOCKSTEP with
# ``crossover_v2.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES``; local
# because that module imports this one.
_HF_MIN_OCCURRENCES: int = 3

# Closed vocabulary of CD-horn continuation suppression reasons. Pinned by a
# test.
HF_SUPPRESSION_REASONS: frozenset[str] = frozenset({
    "insufficient_repeats",
    "repeat_disagreement",
    "fit_quality",
    "no_filter_budget",
})

# Max magnitude error (dB) tolerated between the realized cut-domain cascade
# and the desired cut_target over [onset, ceiling]; above it the stage is
# suppressed (reason="fit_quality"). 2.0 rather than 1.5: the tighter bar
# also caught ordinary curve raggedness on real curves. Not higher: the worst
# mis-shape reached in review probing measured 2.23 dB and must still be caught.
HF_REALIZATION_TOLERANCE_DB: float = 2.0

# Ceiling on CD-horn spend imposed by the SINGLE-Lowshelf realization, dB —
# independent of, and binding below, MAX_NORMALIZATION_SPEND_DB. Measured
# live on JTS3 2026-07-24: the realization passes the quality gate at spend
# 11.27 and fails from ~11.9 upward. Caps how much lift ONE shelf can
# deliver; more needs a different realization (stacked shelves, boost).
HF_SINGLE_SHELF_SPEND_CAP_DB: float = 11.0

# Flatness target for the CD-horn stage's own residual peaking fit —
# TIGHTER than the flattening loop's, since it tracks a SHAPED target. With
# the loose 1.0, design_peq stopped early on a real case (2 of 7 slots,
# 2.18 dB residual, suppressed); at 0.5 the same case lands at 1.27 dB.
_HF_RESIDUAL_FLATNESS_TARGET_DB: float = 0.5

# Max cut (dB) of the "taper" continuation policy's trailing Highshelf.
# Above the confidence ceiling nothing is measurable, so the stage walks the
# relative lift back DOWN by min(spend/2, this).
HF_TAPER_MAX_DB: float = 6.0

# Where the taper's corner sits above the confidence ceiling when there is
# room: far enough that the transition stays unmeasured, close enough to
# still bite inside the band.
_HF_TAPER_CORNER_RATIO: float = 1.25

# The taper's corner must stay strictly below Nyquist or CamillaDSP refuses
# the whole config at load. Derived from the runtime contract's own sample
# rate so a rate change cannot drift this from
# ``camilla_yaml._validated_biquad_entry``.
_HF_TAPER_NYQUIST_HZ: float = DEFAULT_SAMPLE_RATE / 2.0

# Continuation policy above the confidence ceiling, keyed by DECLARED driver
# class — its only remaining authority over the CD-horn stage. "hold":
# nothing extra above the ceiling. "taper": append a trailing Highshelf CUT
# for a rising-breakup metal dome or an unknown driver. Keys MUST cover
# DRIVER_CLASSES exactly (pinned by a test).
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
    """What moves this fit is allowed to make (PR-L5).

    Deliberately small: every field is a move the fit can MAKE, not a fact
    about the speaker. Way count, driver roles, pad authority and alignment
    are the composer's business. ``allow_boost`` is the evidence gate — the
    v2 session grants it only when the commission will run the delta probe.
    """

    allow_boost: bool = False
    #: Per-filter boost ceiling. TOTAL boost is uncapped by design (owner
    #: ruling); this bounds one biquad's realization.
    per_filter_boost_cap_db: float = PER_FILTER_BOOST_CAP_DB
    #: Bands no LIFT filter may be AIMED at (#1967) — enforced per filter on
    #: the emitted response, never as a whole-cascade veto. Cuts untouched.
    #: Empty means "nothing contradicted", not "no evidence available".
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
    """One boost filter the #1967 bound removed, and the arithmetic that
    removed it. ``realized_in_band_db`` is this filter's own realized
    magnitude at its strongest point inside ``band_hz``; dropped because
    that reached at least ``gain_db / 2`` (inside its half-gain bandwidth).
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
    drops — the ACCEPTED, disclosed remainder (skirt tail by construction).
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
    """One boost filter the MEASURED-TARGET bound removed (#2599).
    ``action_band_hz`` is this filter's half-gain bandwidth;
    ``measured_excess_db`` is how far the MEASURED response sits above
    target at the least-hot bin there — dropped because that is ``>= 0``
    everywhere it acts.
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
    measurement covers (#2599). A disclosure, not a refusal.
    ``blind_band_hz`` is the hole; ``measured_excess_db`` is
    ``smoothed - target`` at that centre.
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
    """RBJ Audio EQ Cookbook Highshelf magnitude response, in dB, at
    ``freqs_hz`` for a filter designed at ``corner_hz``/``gain_db``/``q``.

    The same digital biquad family CamillaDSP realizes, at
    :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`. Separate from
    ``sound.profile._filter_response_db`` — Highshelf-only, vectorized,
    returns an ndarray (the shape this fit loop needs).
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
    (not :class:`jasper.correction.peq.PEQ`, which has no ``biquad_type``).
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


#: The persisted key naming WHICH MICROPHONE measured the round this entry
#: came from. A named constant because a second writer emits it too
#: (``driver_prescription_to_candidate_fields``) and a spelling drift would
#: silently remove ``CrossoverV2Session._mic_trust_ceiling_hz``'s grading
#: ceiling rather than fail (#2649).
MIC_TIER_FIELD = "mic_tier"


@dataclass(frozen=True)
class LinearizationFit:
    """One driver's fitted linearization — the Layer-1a artifact.

    ``fit_band_hz == (0.0, 0.0)`` signals no fit was attempted (the envelope
    allowed correction nowhere); ``filters`` is empty. ``verify_*`` and
    ``observe_octave_summary`` are the honesty ladder's other two levels:
    FIT claims accuracy strictly inside the fit band; VERIFY applies the
    SAME residual math roughly an octave past it; OBSERVE is per-octave
    achieved-vs-target to the grid's own top. All REPORT-ONLY. Measured on
    the REALIZED cascade (:func:`complex_correction_response`, the exact
    RBJ biquads CamillaDSP emits), never the internal Lorentzian.

    FIT DIAGNOSTICS, per-driver — not the flat-linearization spec claim,
    which is graded on the spatially-combined cloud by
    :func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`. The two
    answer different questions on different curves and may disagree.
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
    # CD-horn compensation stage (#1668); all default to the zeroed/empty
    # "did-not-fire" state. ``measured_deficit_at_ceiling_db`` reports the
    # UNCAPPED measured deficit at the ceiling.
    hf_continuation_spend_db: float = 0.0
    hf_continuation_ceiling_hz: float = 0.0
    hf_continuation_policy: str = ""
    hf_continuation_suppressed_reason: str = ""
    measured_deficit_at_ceiling_db: float = 0.0
    # LEVEL this driver's correction removed from its own reference (core)
    # band, POSITIVE dB — MEASURED before-vs-after power-domain average over
    # ``_core_or_fallback_mask``. The SSOT for the AUDIBLE-BAND give-back;
    # does NOT place the trim (``plan_linearization`` anchors on
    # ``branch_level_bands_hz`` instead — using this one shipped the jts3
    # horn tweeter 3.67 dB hot, 2026-08-19). 0.0 when no filters emitted.
    correction_giveback_db: float = 0.0
    # PR-L5 disclosure, #1808 charge: realized peak of the branch chain this
    # fit is emitted into, plus ``branch_chain.HEADROOM_MARGIN_DB`` — exactly
    # what the emitter CHARGES to ``active_baseline_headroom``. Stamped by
    # the composer, not computed here (this core does not know its chain).
    headroom_cost_db: float = 0.0
    # Lift the boost vocabulary was asked for and delivered, dB, over the
    # fit band. ``lift_from_reduced_cuts_db`` is the share bought by
    # SHRINKING this fit's own cuts (free).
    lift_requested_db: float = 0.0
    lift_from_reduced_cuts_db: float = 0.0
    lift_from_boost_db: float = 0.0
    lift_suppressed_reason: str = ""
    # #1967's boost-evidence bound: one record per boost filter REMOVED for
    # overlapping a contradicted band, and what the surviving cascade still
    # puts inside each band (accepted skirt). A whole-lift refusal also sets
    # ``lift_suppressed_reason``; a partial one does not.
    lift_boost_excluded_drops: tuple[BoostExclusionDrop, ...] = ()
    lift_boost_excluded_residual: tuple[BoostExclusionResidual, ...] = ()
    # #2599's two measured-evidence bounds. ``lift_boost_evidence_drops``:
    # one record per boost removed because the MEASURED response was
    # already at/above target everywhere it acts (empty result sets
    # ``lift_suppressed_reason`` to ``"boost_above_measured_target"``).
    # ``blind_zone_placements``: one record per EMITTED Peaking filter
    # centred in a span no branch's capture covers — ships, only NAMED.
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

    A branch correction is SUMMED through the (phase-dominated) crossover, so
    a zero-phase magnitude model mispredicts it and can land FURTHER off than
    omitting the filters entirely (measured on JTS3, #1667: 2.0 dB
    mistracking vs. 1.7 dB uncorrected, vs. ~0.5 dB for this complex model).
    Every entry is the exact RBJ biquad CamillaDSP realizes, IMPORTED from
    :func:`jasper.sound.profile._filter_response_complex` rather than
    re-derived. Apply in the LINEAR domain: ``W_lin = W *
    complex_correction_response(...)``.
    """
    return chain_response([f.to_dict() for f in filters], freqs_hz)


def linearization_filters_by_role(
    linearization_mapping: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Reduce a persisted ``{role: LinearizationFit.to_dict()}`` mapping to
    the emitter's input shape: ``{role: [filter_dict, ...]}``.

    ``baseline_profile.recompose_applied_baseline_yaml`` deliberately does
    NOT call this: its snapshot is already in this function's OUTPUT shape,
    and calling this on an already-reduced mapping silently returns ``{}``
    for every role — pinned by
    ``test_linearization_filters_by_role_on_already_reduced_shape_is_empty``.

    Defensive, not authoritative: the emitter's ``_validated_linearization``
    is the fail-closed gate.
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


# What a stored ``headroom_cost_db`` MEANS, per era (#1808). The charge's
# derivation changed twice and is NOT re-derived on load
# (docs/historical/linearization-campaign-2026-07.md, "Cross-era
# disclosure") — the era must travel WITH the number, recorded never
# inferred. Era 1: SUM of positive filter gains (a loose upper bound). Era
# 2: the realized peak. Era 3 (#2758): the peak's grid now spans the whole
# domain, so an old stamp can read smaller than re-emitting today charges.
# Absent means UNKNOWN, never a default.
HEADROOM_COST_BASIS_REALIZED_PEAK = "realized_peak"
HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN = "realized_peak_full_domain"
HEADROOM_COST_BASIS_UNKNOWN = "unknown"


def worst_headroom_cost_db(linearization_mapping: Mapping[str, Any]) -> float:
    """The max-level cost of a whole correction, dB — the WORST branch's
    :attr:`LinearizationFit.headroom_cost_db` (PR-L5).

    Worst branch and not the sum, matching
    ``camilla_yaml.linearization_headroom_db``: driver chains run in
    PARALLEL after the split, so no sample path sees two branches' boosts.
    Defensive like :func:`linearization_filters_by_role`: a malformed or
    era-older entry is skipped rather than raising.
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
    ``10*log10(mean(10**(dB/10)))``. Returns 0.0 on an empty mask. Same
    power-domain mean the trim solver uses, so
    :attr:`LinearizationFit.correction_giveback_db` stays comparable to it.
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
        # Honesty-ladder levels 2/3 are degenerate placeholders too: no fit
        # was attempted.
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
    claim uses, over ``[fit_lo_hz, min(2*fit_hi_hz, grid_top)]`` — roughly
    an octave past the fit band's top. Report-only. ``target_curve_db`` is
    per-bin since R10a (#1817): a flat array would score a branch's own
    crossover rolloff as residual through the handoff.
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
    Mirrors :func:`_octave_band_reason_summary`'s octave-center sampling so
    the two dicts key identically. ``target_curve_db`` per-bin since R10a —
    a flat target over the whole grid would report a two-way branch's
    stopband attenuation as a deficit of tens of dB.
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
    sit at the ceiling sentinel — intersected with the fit-eligible mask.
    Falls back to the whole fit-eligible mask when the core is empty.
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
#: This fit ladder-smooths at 1/6 octave below 4 kHz, so a third of an
#: octave is two smoothing kernels — the narrowest span where "the median"
#: is a statistic rather than one smoothed point (~8 bins on the production
#: grid). Protects against a DISCONTINUITY: measured on a flat tweeter, a
#: raw intersection swung 16.55 dB across 25 Hz of crossover frequency
#: against a 3.0 dB gate tolerance. A sub-floor intersection is WIDENED to
#: this width (downward from its top edge, upward from the core's bottom
#: bin if that runs out of room) rather than discarded to the whole-mask
#: fallback, which would move the cliff into ordinary two-way territory.
#: :func:`core_level_band_hz` discloses the two cases that still take the
#: whole mask: an EMPTY intersection, and a core mask narrower than the floor.
_MIN_LEVEL_BAND_OCTAVES: float = 1.0 / 3.0


def _spans_floor(lo_hz: float, hi_hz: float) -> bool:
    """Is ``[lo_hz, hi_hz]`` at least :data:`_MIN_LEVEL_BAND_OCTAVES` wide?"""
    return lo_hz > 0.0 and math.log2(hi_hz / lo_hz) >= _MIN_LEVEL_BAND_OCTAVES


def _core_level_mask(
    envelope: EnvelopeCurve,
    envelope_mask: np.ndarray,
    radiating_band_hz: tuple[float, float] | None,
) -> np.ndarray:
    """The bins a core-level median runs over: the core mask, narrowed to
    ``radiating_band_hz``, widened back to :data:`_MIN_LEVEL_BAND_OCTAVES`
    if that narrowing left less band than a median can be taken over. THE
    one implementation — :func:`driver_core_level_db` and
    :func:`core_level_band_hz` both bottom out here. An EMPTY intersection
    falls back to the whole core mask (a three-way mid squeezed between two
    crossovers has no radiating band); :func:`core_level_band_hz` discloses
    when this happened.
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

    # Sub-floor. Widening runs on the core mask's OWN bins, each edge
    # snapped OUTWARD to the first bin reaching the floor width.
    core_idx = np.flatnonzero(core)
    core_freqs = grid_hz[core_idx]
    span = 2.0 ** _MIN_LEVEL_BAND_OCTAVES

    # DOWN from this intersection's own top edge first (the tweeter case).
    top = int(np.searchsorted(core_freqs, hi_used, side="right")) - 1
    bottom = int(np.searchsorted(core_freqs, hi_used / span, side="right")) - 1
    if bottom < 0:
        # Downward room exhausted (the woofer case) — make the deficit up
        # UPWARD from the core's own bottom bin.
        bottom = 0
        top = int(np.searchsorted(core_freqs, core_freqs[0] * span, side="left"))
        if top >= core_freqs.size:
            # Neither direction has room: core mask narrower than the floor.
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
    """One driver's own PASSBAND level — the starting estimate, not the
    level fact. Runs :func:`fit_driver_linearization`'s own resample ->
    ladder-smooth -> core-mask -> median chain, exposed separately because
    it is read across ALL drivers before any one is fitted.

    Demoted and kept (ruling S8): "Level-matched" means matched acoustic
    output through the HANDOVER REGION (``solve_branch_trims``'s power mean
    over mirrored +/-1-octave halves about Fc), a different quantity from
    this passband-average sensitivity — on a sloped horn they legitimately
    differ by many dB, disclosed and never reconciled. Does not place the
    trim pair; since #2609 nothing derived from it does.

    ``radiating_band_hz`` (#1929) narrows the median to where this driver's
    crossover leaves it radiating; ``None`` is the pre-#1929 whole-core-mask
    median byte for byte. Subject to :func:`_core_level_mask`'s width floor;
    :func:`core_level_band_hz` reports which way that went. The band
    matters because the core mask's declared ``measurement_band_hz``
    routinely reaches past Fc, and a MEDIAN (rank statistic) lets a
    stopband bin count as much as a passband one (2026-07-30 JTS3: a woofer
    read 3.4 dB away from the trim solve for exactly this reason).

    Returns ``None`` — not a number — when the envelope allows correction
    nowhere: UNKNOWN, not a placeholder that would move every other driver.
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
    for the same arguments — ``None`` when it would return ``None``. Lets a
    caller disclose the realized band rather than the bound it asked for; a
    difference wider than a bin means :func:`_core_level_mask`'s width
    floor fired, a sub-bin one is grid quantization.
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
    """Adaptive fit-band trim. Returns inclusive ``(lo_idx, hi_idx)`` grid
    indices. The seed is CURVE-SHAPE-DRIVEN, not trust-driven — the extremes
    of ``envelope_mask`` bins within one cut budget of ``target_level_db``
    — deliberately not the mic-trust/class-prior "core" region, since a
    driver's acoustic rolloff has nothing to do with mic trust. From that
    seed it extends outward, stopping the first time the smoothed curve
    drops below the floor or the mask ends.
    """
    idxs = np.flatnonzero(envelope_mask)
    floor_db = target_level_db - PER_FILTER_CUT_CAP_DB
    within_budget = envelope_mask & (smoothed_db >= floor_db)
    seed_idxs = np.flatnonzero(within_budget)
    if seed_idxs.size:
        seed_lo, seed_hi = int(seed_idxs[0]), int(seed_idxs[-1])
    else:
        # Degenerate: nothing within budget. Seed from the closest bin so
        # both loops collapse to a 1-bin band rather than crashing.
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
    """``band_mask`` narrowed to the bins this branch still materially
    reaches the summed response in (#2523) — the declared radiating band
    widened by
    :data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`.
    This is the band the solve's OBJECTIVE runs over; bins outside it are
    EXCLUDED, not zero-targeted (a zeroed target is still a demand a greedy
    ``design_peq`` will chase). Widened because masking at the -3 dB edge
    refuses cuts twice measured as genuine work; the margin is the same
    half octave ``BranchTarget.gain_band_hz`` widens by. ``None`` or an
    EMPTY intersection narrows nothing (same fallback as
    :func:`_core_level_mask`).
    """
    if radiating_band_hz is None:
        return band_mask
    lo_hz, hi_hz = radiating_band_hz
    narrowed = band_mask & (
        (grid_hz >= octave_scaled(lo_hz, -STOPBAND_GAIN_MARGIN_OCTAVES))
        & (grid_hz <= octave_scaled(hi_hz, STOPBAND_GAIN_MARGIN_OCTAVES))
    )
    return narrowed if narrowed.any() else band_mask


# A falling top octave is compensated by ``_hf_continuation_stage`` below;
# ``_shelf_stage`` stays the RISING-slope Highshelf, mutually exclusive.
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
    faster than :data:`SHELF_SLOPE_THRESHOLD_DB_PER_OCT`. ``None`` when no
    shelf is warranted. ``shape_db`` (R10a, #1817) is the branch's
    re-centred crossover shape; the regression runs on ``smoothed_db -
    shape_db`` so the gate is armed by the branch's OWN slope, not the
    crossover's (a flat tweeter behind a 2 kHz LR4 reads +5.6957 dB/oct raw
    against a shape-removed 0.0000). ``None`` regresses the raw curve.
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

    # Normalization-budget clamp: budget left after the plateau-vs-target gap.
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

    ``filters`` is empty on every non-firing path. When the stage FIRES it
    is ``(lowshelf, *peaking_cuts, taper?)`` in the emitter's shelf-first /
    taper-last order. ``suppressed_reason`` is the sole non-empty field on
    an objective-gate suppression.
    """

    filters: tuple[LinearizationFilter, ...]
    spend_db: float
    ceiling_hz: float
    policy: str
    suppressed_reason: str
    measured_deficit_at_ceiling_db: float


# The empty/zeroed "did not fire, no objective suppression" result.
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
    ``ceiling_hz`` is the first bin where mic-trust reaches ~0 (or
    ``grid_hz[-1]`` if it never does). ``knee_hz`` is the first bin BELOW
    the ceiling sentinel — the same test :func:`_core_or_fallback_mask`
    uses, so "still fully trusted" means one thing across this module.
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
    """The repeat-agreement gate: ``""`` when the repeats agree well enough
    over the compensation band, else a suppression reason. Spread is the
    per-bin ``max - min`` across ALL sweep occurrences (primary + repeats,
    matching ``compute_sigma_curve``) — the primary must be included, or an
    outlier the repeats don't reproduce sizes a several-dB-too-hot lift.
    Fewer than :data:`_HF_MIN_OCCURRENCES` occurrences suppresses.
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
    Sizes a lift from the MEASURED deficit (class-blind), realizes it in
    the CUT domain — cut below the compensation region by ``spend`` so the
    flow's trim re-solve levels the branches back and the top octave lands
    ``spend`` dB higher RELATIVELY — and, only above the confidence
    ceiling, applies a declared-class continuation policy. Objective gates
    suppress it rather than ship a guess. Every emitted filter is a cut.
    """
    mic_trust = envelope.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    ceiling_hz, knee_hz = _hf_confidence_ceiling_and_knee_hz(grid_hz, mic_trust)

    # Applicability: a woofer/mid topping out below the mic knee has no
    # top-octave deficit; a rising-slope Highshelf already emitted means
    # mutual exclusivity. Both inert, no reason.
    if fit_hi_hz < knee_hz:
        return _HF_INERT
    if any(f.biquad_type == "Highshelf" for f in filters):
        return _HF_INERT
    # Applies but the flattening loop spent every slot — named, not silent.
    if len(filters) >= MAX_FILTERS_PER_DRIVER:
        return _hf_suppressed("no_filter_budget")

    disagreement = _hf_repeat_spread_ok(grid_hz, primary, ceiling_hz)
    if disagreement:
        return _hf_suppressed(disagreement)

    ceiling_idx = int(np.argmin(np.abs(grid_hz - ceiling_hz)))

    # Desired compensation C(f), against the target CURVE since R10a (#1817).
    deficit_db = target_curve_db - working_db
    measured_deficit_at_ceiling_db = float(max(0.0, deficit_db[ceiling_idx]))

    # Onset: first bin above the trusted band's geometric midpoint where the
    # smoothed deficit rises through _MIN_FILTER_GAIN_DB and stays positive
    # to the ceiling — a contiguous falling region, not a lone blip.
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
        return _HF_INERT
    onset_hz = float(grid_hz[onset_idx])

    remaining_budget_db = max(
        0.0, MAX_NORMALIZATION_SPEND_DB - (plateau_level_db - target_level_db)
    )
    # Three independent ceilings: measured deficit, remaining ledger
    # budget, and what a SINGLE Lowshelf can realize (binding today).
    spend = min(
        measured_deficit_at_ceiling_db,
        remaining_budget_db,
        HF_SINGLE_SHELF_SPEND_CAP_DB,
    )
    if spend < _MIN_FILTER_GAIN_DB:
        return _HF_INERT

    # C(f): 0 below onset, rescaled to hit exactly ``spend`` at the
    # ceiling, held at ``spend`` above it (correction never RISES past
    # confidence).
    scale = spend / measured_deficit_at_ceiling_db
    compensation_db = np.zeros_like(grid_hz)
    band = (np.arange(len(grid_hz)) >= onset_idx) & (np.arange(len(grid_hz)) <= ceiling_idx)
    compensation_db[band] = np.clip(
        np.maximum(0.0, deficit_db[band]) * scale, 0.0, spend
    )
    compensation_db[np.arange(len(grid_hz)) > ceiling_idx] = spend
    # Cut-domain transform: cut_target <= 0 everywhere.
    cut_target_db = compensation_db - spend

    # Cut-domain realization: Lowshelf backbone + peaking residual. The
    # shelf's gain is CLAMPED at PER_FILTER_CUT_CAP_DB — past the cap the
    # shelf carries the first 12 dB and the peaking residual absorbs the rest.
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

    # Fit the residual with peaking cuts in the TRUSTED band; the top
    # octave gets NO peaking filter — its lift arrives via the give-back.
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

    # Fit-quality check: realized cut cascade must track cut_target across
    # [onset, ceiling] within HF_REALIZATION_TOLERANCE_DB, or suppress.
    realized = tuple([lowshelf, *hf_peaks])
    realized_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(realized, grid_hz)), 1e-12)
    )
    check_band = (grid_hz >= onset_hz) & (grid_hz <= ceiling_hz)
    if check_band.any():
        max_err = float(np.max(np.abs(realized_db - cut_target_db)[check_band]))
        if max_err > HF_REALIZATION_TOLERANCE_DB:
            return _hf_suppressed("fit_quality")

    # Continuation policy above the ceiling (declared class's authority).
    emitted = [lowshelf, *hf_peaks]
    if (
        policy == "taper"
        and len(filters) + len(emitted) < MAX_FILTERS_PER_DRIVER
        and ceiling_hz < _HF_TAPER_NYQUIST_HZ
    ):
        # One trailing Highshelf CUT above the ceiling, appended LAST
        # (taper-last contract). Corner clamped, never skipped, between
        # ABOVE ceiling_hz and strictly BELOW Nyquist (where
        # ``_validated_biquad_entry`` would refuse the config).
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

# Bisection iterations to find how far one existing cut can be shrunk
# without overshooting the desired lift. 24 halvings take a 12 dB range
# well under a micro-dB.
_CUT_REDUCTION_BISECTION_STEPS: int = 24

# Materiality slack (dB) on the permitted-headroom test. NOT float noise: a
# far-field leakage bin with fractionally negative headroom vetoed an
# entire shrink at 1e-6 (measured: -0.0034 dB bin delivered 0.00 of a
# wanted 4.00 dB). Reuses :data:`_ENVELOPE_NONZERO_EPS_DB`.
_CUT_REDUCTION_EPS_DB: float = _ENVELOPE_NONZERO_EPS_DB


def reduce_cuts_for_lift(
    filters: Sequence[LinearizationFilter],
    wanted_db: np.ndarray,
    headroom_db: np.ndarray,
    grid_hz: np.ndarray,
) -> tuple[tuple[LinearizationFilter, ...], np.ndarray]:
    """Spend a desired lift by SHRINKING cuts we ourselves placed, before
    any boost is considered. Returns ``(filters, delivered_lift_db)``.
    Reducing a cut is free, uses no slot, and is exactly invertible —
    unlike two filters fighting each other. Two arrays, two jobs:
    ``wanted_db`` is how much lift would be USEFUL (``>= 0``); ``headroom_db``
    is how much is PERMITTED (may be negative or ``+inf``) — the SAFETY
    constraint stopping a cut placed to tame a peak from unwinding to fill
    an unrelated dip. Greedy, deepest cut first, by bisection; delivered
    lift uses the real RBJ evaluator, not a linear approximation.
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
    """Result of :func:`_lift_stage`. ``filters`` is the WHOLE post-stage
    cascade (it may have shrunk existing cuts, so it cannot return only
    its additions)."""

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
    """Split ``boosts`` into the ones the MEASUREMENT supports and the ones
    it contradicts (#2599). Returns ``(kept, dropped)``.
    ``measured_headroom_db`` is ``target − measured``: positive is a
    measured deficit, zero or negative an excess where adding level makes
    it worse. The criterion is the filter's own half-gain bandwidth (same
    intrinsic, scale-free test :func:`_boost_exclusion_verdicts` uses); a
    boost is dropped only when headroom is ``<= 0`` at EVERY bin of its
    action region. Graded against MEASURED, not ``working`` (the
    measurement plus every cut placed above) — a cut's skirts can drag
    ``working`` down and manufacture a deficit the measurement never had.
    """
    kept: list[LinearizationFilter] = []
    dropped: list[BoostEvidenceDrop] = []
    for boost in boosts:
        own_db = 20.0 * np.log10(np.maximum(
            np.abs(complex_correction_response((boost,), grid_hz)), 1e-12,
        ))
        action = (own_db >= boost.gain / 2.0) & claim_mask
        if not np.any(action):
            # Nothing the fit makes a claim over — this bound abstains.
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
            # The LEAST-hot bin it acts on: the closest this filter came to
            # having something to fill, and it still had nothing.
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

    Per filter, intrinsic, relative — read once against its OWN transfer
    function, no ordering, no "drop until it fits" (the arbitrary-ordering
    hazard ``interference_nulls.EXCLUSION_CAP_FRACTION`` warns about). The
    criterion is the filter's own half-gain bandwidth (scale-free: a +1 dB
    bell centred in the band goes, an +11.67 dB bell 0.7 octaves away is
    spill and stays). Not an absolute dB threshold — one calibrated for the
    stopband guard refused 94.4% of randomized multi-dip fits when tried
    here.

    Returns ``(kept, dropped, residual)``. ``residual`` carries the realized
    max still inside each band AFTER the drops — skirt tails by
    construction, disclosed rather than refused.
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
            # One record per DROPPED FILTER, naming the band it overlaps most.
            dropped.append(BoostExclusionDrop(
                band_hz=aimed[0], freq_hz=float(boost.freq), q=float(boost.q),
                gain_db=float(boost.gain), realized_in_band_db=aimed[1],
            ))

    # Hoisted: the surviving cascade doesn't change between bands.
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
    """The spans NO branch's own measured core band covers (#2599, #2600
    item 4) — gaps between the supplied :func:`core_level_band_hz` bands,
    ascending. Between the woofer's top and the tweeter's bottom neither
    per-branch capture carries trusted evidence while the SUMMED response
    there is the phase-sensitive blend of both.
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
    """Name every emitted Peaking filter CENTRED in a span no branch
    measured (#2599). Reports; refuses nothing.

    Run over the FINAL cascade, cuts and lift boosts alike — reading one
    stage's prescriptions instead let 74 hole-centred boosts ship unnamed
    across a 400-fit probe. Reports rather than refuses: no band criterion
    separates a legitimate cut from a bad one here (clamping to each
    branch's own core band would also reverse #1809's ruling that a cut
    past the handoff is ordinary useful work) — the honest separator needs
    the SUM, which only the alignment/crossover layer sees. Peaking only: a
    shelf's ``freq`` is a CORNER, not a placement.
    """
    if not blind_bands_hz:
        return ()
    placements: list[BlindZonePlacement] = []
    for emitted in filters:
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
    stages. Two moves in order: :func:`reduce_cuts_for_lift` (free), then
    boost filters for the residue if the vocabulary allows. Null exclusion
    still binds via ``envelope.allowed_depth_db``. Inert under a cut-only
    vocabulary, not as a formality — chasing every below-target dip would
    silently change what every pre-PR-L5 caller gets. ``contribution``
    (R10a, #1968) scales the WANTED deficit by the branch's own-output
    fraction, gain side only.

    Three bounds, applied in order: the stopband-gain guard (#1968,
    structural — a REALIZED-response check over the WHOLE grid, since a
    bell's skirts reach past its own radiating edge, refused as
    ``"stopband_gain"``); the measured-target bound (#2599, per filter,
    against ``target_curve_db − measured_db`` — see
    :func:`_boost_evidence_verdicts`); the boost-evidence bound (#1967, per
    filter, dropping boosts overlapping a contradicted band — see
    :func:`_boost_exclusion_verdicts`). Suppressed (named, never silent)
    when no slots remain, ``design_peq`` cannot realize the residue, the
    realized lift overshoots the envelope's allowance or puts gain in the
    stopband, or every boost is refused.
    """
    if not vocabulary.allow_boost:
        return _Lift(tuple(filters), 0.0, 0.0, 0.0, "")

    # PERMITTED lift per bin: distance to target inside the fit band
    # (negative above it), unconstrained outside — read over the whole fit
    # band, not ``lift_mask``, since a skirt must not overshoot past handoff.
    headroom_db = np.where(band_mask, target_curve_db - working_db, np.inf)
    # WANTED lift: the positive part, bounded by the envelope's allowance.
    # ``lift_mask`` narrows this to the driver's own radiating band (#1809),
    # making the bound boost-only; defaults to the fit band.
    wanted_mask = band_mask if lift_mask is None else lift_mask
    deficit_db = np.clip(
        np.where(wanted_mask, target_curve_db - working_db, 0.0), 0.0, None,
    )
    if contribution is not None:
        # #1968's contribution weighting, gain side only.
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

    # Boost designer's band is the LIFT band, so a centre can never land
    # where this driver has handed off.
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
        # Explicit: bounds the #1967 drop radius. See _PEAKING_Q_MIN.
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

    # Realization gate: the cascade to be emitted must stay inside the
    # envelope's per-bin allowance (a greedy bell fit can overshoot between
    # centres).
    realized_db = 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(tuple(boosts), grid_hz)), 1e-12)
    )
    allowance = np.maximum(envelope.allowed_depth_db, 0.0)
    if np.any(realized_db[band_mask] > allowance[band_mask] + _MIN_FILTER_GAIN_DB):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "exceeds_envelope",
        )

    # #1968's hard rule: no significant gain past this branch's passband
    # edge, read over the WHOLE grid, not ``band_mask`` (see docstring).
    if gain_permitted is not None and np.any(
        realized_db[~gain_permitted] > SIGNIFICANT_GAIN_DB
    ):
        return _Lift(
            tuple(reduced), requested_db, from_reduced_cuts_db, 0.0,
            "stopband_gain",
        )

    # #2599's measured-target bound, per filter, placed AFTER both
    # whole-cascade gates above so a refused cascade cannot return as an
    # accepted subset.
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
    # Gain surviving INSIDE an excluded band is now skirt tail, disclosed
    # rather than refused.
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
    """Fit one driver's linearization from its measured response and
    correction envelope.

    Cut-preferred, not cut-only: ``vocabulary`` decides whether a lift is
    admitted, and this function re-proves that decision and the per-filter
    boost cap on its own output before returning.

    ``radiating_band_hz`` bounds two things at two widths. LIFT is bounded
    at the band ITSELF (#1809): a driver spending GAIN against its own
    crossover is a pathology (2026-07-28 JTS3 woofer: +11.6155 dB at 2747
    Hz for +1.06 dB net contribution). The SOLVE is bounded at that band
    widened by
    :data:`~jasper.active_speaker.branch_target.STOPBAND_GAIN_MARGIN_OCTAVES`
    (#2523). ``None`` means unbounded (a one-way box).

    ``blind_bands_hz`` (#2599): spans NO branch's own measured core band
    covers — a peaking filter centred there is not refused, only NAMED.

    ``target`` is the target level's SHAPE (#1817) — a
    :class:`~jasper.active_speaker.branch_target.BranchTarget` re-centred to
    add no level over the band ``target_level_db`` is the median of, so
    only the per-bin GRADING moves. ``None`` is the flat target byte for
    byte. This fit is independent of the level datum: a driver is
    flattened to its OWN passband; where that passband is PLACED relative
    to the others is a trim decided later and elsewhere.

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

    # The target's SHAPE (#1817), re-centred on the SAME mask the scalar
    # above is the median of. Explicit raise, not `assert` (stripped under
    # `python -O`) — this is hardware-bound output.
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

    # THE SOLVE BAND (#2523): the fit band narrowed to where this branch
    # still materially reaches the summed response — see
    # :func:`_solve_band_mask`. A real branch flattens into breakup,
    # leakage and noise floor past its ideal digital crossover, and every
    # dB of that gap otherwise arrives at the solve as a demand (pre-#2523,
    # a branch declared to radiate only to 1282.3 Hz spent all 8 slots
    # between 9.7-11.8 kHz). Bounds only what the solver is FED, never
    # ``level_mask`` or ``_observe_octave_summary`` (the DISCLOSURE layer).
    band_mask = _solve_band_mask(grid_hz, band_mask, radiating_band_hz)
    # Re-read the band's edges off the mask the stages actually run on, so
    # every downstream band agrees with it.
    solve_idx = np.flatnonzero(band_mask)
    fit_lo_idx, fit_hi_idx = int(solve_idx[0]), int(solve_idx[-1])

    # Where LIFT may go (#1809): the fit band clamped to the radiating side
    # of the crossover. Boosts only — a CUT outside the radiating band is
    # ordinary useful work (spends no headroom, can't fight the
    # crossover), where a BOOST there is attenuated by the crossover it
    # fights and charges full headroom for nothing.
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

    # CD-horn compensation stage (#1668), runs AFTER the peaking loop.
    hf = _hf_continuation_stage(
        grid_hz, working_db, target_curve_db, target_level_db, plateau_level_db,
        envelope, primary, fit_lo_hz, fit_hi_hz, filters,
    )
    if hf.filters:
        # Lowshelf backbone to position 0 (shelf-before-peaks contract).
        filters = [hf.filters[0], *filters, *hf.filters[1:]]
        working_db = working_db + 20.0 * np.log10(
            np.maximum(np.abs(complex_correction_response(hf.filters, grid_hz)), 1e-12)
        )

    # Lift stage (PR-L5), runs LAST, in the CD-horn's give-back frame if it fired.
    lift = _lift_stage(
        grid_hz, working_db, target_curve_db - hf.spend_db, envelope,
        band_mask, filters, vocabulary,
        # The MEASUREMENT, not the working curve — #2599's bound exists
        # because the two disagree once cuts are placed.
        measured_db=smoothed_db,
        lift_mask=lift_mask,
        contribution=None if centred_target is None else centred_target.contribution,
        gain_permitted=(
            None if centred_target is None else centred_target.gain_permitted
        ),
    )
    filters = list(lift.filters)

    # Restore the emitter's taper-last contract: ``_lift_stage`` appends
    # boosts, so a trailing CD-horn taper stops being trailing whenever one
    # survives. A fit carries at most one Highshelf, so any past index 0 is it.
    taper_at = next(
        (i for i, f in enumerate(filters) if i and f.biquad_type == "Highshelf"),
        None,
    )
    if taper_at is not None and taper_at != len(filters) - 1:
        filters.append(filters.pop(taper_at))

    # THE #2599 PLACEMENT SITE, read AFTER the lift stage, on the FINAL
    # list (the flattening loop's prescriptions alone let 74 hole-centred
    # lift boosts ship unnamed). Graded against ``target_curve_db``, NOT
    # the give-back frame.
    blind_zone_placements = _blind_zone_placements(
        filters, grid_hz, smoothed_db - target_curve_db, blind_bands_hz,
    )

    # THE CLAIM SEAM (R10b): everything below is a REPORTED NUMBER, graded
    # against the cascade that will actually emit. Rebuilding from
    # ``smoothed_db`` plus the WHOLE cascade rather than carrying
    # ``working_db`` forward avoids double-counting a lift-stage shrink and
    # avoids grading against the peaking stage's internal Lorentzian
    # approximation (:func:`complex_correction_response` is the exact
    # biquad). Unconditional since R10b — a cut-only vocabulary is graded
    # by the same exact evaluator. Does NOT reach the claim FRAME (#2013):
    # ``frame_target_db`` below carries ``hf.spend_db``, sized above this
    # line against the Lorentzian-folded ``working_db``.
    working_db = smoothed_db + 20.0 * np.log10(
        np.maximum(
            np.abs(complex_correction_response(tuple(filters), grid_hz)), 1e-12
        )
    )

    # Explicit raise, not `assert` (stripped under `python -O`): a safety
    # invariant on hardware-bound output.
    if not vocabulary.allow_boost and any(f.gain > 0.0 for f in filters):
        raise RuntimeError("linearization fit emitted a boost under a cut-only vocabulary")

    # Per-filter caps are HARD invariants; re-prove here rather than trust
    # each stage's own clamp.
    if any(f.gain < -PER_FILTER_CUT_CAP_DB - 1e-6 for f in filters):
        raise RuntimeError("linearization fit exceeded the per-filter cut cap")
    if any(
        f.gain > vocabulary.per_filter_boost_cap_db + 1e-6 for f in filters
    ):
        raise RuntimeError("linearization fit exceeded the per-filter boost cap")

    # MEASURED before-vs-after core-band level delta; does not anchor a
    # trim (the anchor measures over ``branch_level_bands_hz`` instead).
    correction_giveback_db = 0.0
    if filters:
        correction_giveback_db = (
            _power_band_average_db(smoothed_db, level_mask)
            - _power_band_average_db(working_db, level_mask)
        )

    # Give-back frame: when the CD-horn stage fired, the honest reference
    # for claims below is the target curve MINUS spend (0 if it didn't fire).
    frame_target_db = target_curve_db - hf.spend_db
    residual = (working_db - frame_target_db)[band_mask]
    residual_rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    residual_max_db = float(np.max(np.abs(residual))) if residual.size else 0.0

    # Honesty-ladder levels 2/3, over the SAME working_db and give-back
    # frame the FIT claim used, just wider bands.
    verify_band_hz, verify_residual_rms_db, verify_residual_max_db = (
        _verify_band_and_residual(
            grid_hz, working_db, frame_target_db, fit_lo_hz, fit_hi_hz,
        )
    )
    observe_octave_summary = _observe_octave_summary(
        grid_hz, working_db, frame_target_db,
    )

    # Octave centers ABOVE the confidence ceiling are disclosed as
    # beyond-measurement-confidence when the CD-horn stage fired.
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
