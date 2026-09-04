# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tuning constants and the result records of a program-capture analysis."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.quality_model import DRIVER


# ``__package__``, not ``__name__``: every submodule logs under the one
# pre-split name that caplog filters and the operator runbook cite.
logger = logging.getLogger(__package__)

# --- locator / drift / alignment tuning ---
# Per-segment search half-window, s, around the drift-free scheduled offset:
# a few-hundred-ppm drift over a ~25 s program is ≈6 ms, plus acoustic delay.
SEGMENT_SEARCH_S = 0.030
# A stuck recording is truncated to program duration + this margin (1 GB Pi;
# mirrors deconv.cap_capture_length) before any full-rate FFT runs.
CAPTURE_BOUND_MARGIN_S = 10.0
# Downsampled rate for the global-offset locate, so the whole-capture
# correlation never allocates hundreds of MB.
LOCATOR_RATE_HZ = 16_000
# Clip run: consecutive samples at/above full scale (threshold owned by quality_model).
CLIP_RUN_SAMPLES = 3
CLIP_ABS_THRESHOLD = DRIVER.clip_abs_threshold
DBFS_FLOOR = -120.0
ILL_CONDITIONED_PROTECTION_DEEMBEDDING = "ill_conditioned_protection_deembedding"
# Conditioning floor on the emitted protection `P`, dB: below it, dividing it
# out amplifies noise faster than it recovers signal.
CONFIGURED_PATH_PROTECTION_FLOOR_DB: float = -12.0


class ConfiguredPathConditioningError(ValueError):
    slug = ILL_CONDITIONED_PROTECTION_DEEMBEDDING

    def __init__(self, detail: str, *, protection_floor: bool = False) -> None:
        self.protection_floor = protection_floor
        super().__init__(f"{self.slug}: {detail}")


# Max disagreement between drift baselines, samples-equivalent (§5.6.3):
# measured `_subsample_separation` floor over a 28-capture corpus is
# 0.04-0.30 samples; a +4-sample insertion rejects, +2 passes.
GLITCH_RESIDUAL_SAMPLES = 1.5
MAX_DRIFT_PPM = 500.0

# Max captured-level gap, dB, between MEASURE's two bit-identical woofer
# sweeps (design §5.2); read as band-relative in-band RMS, not full-band
# peak — two hardware mics read identical sweeps 0.64 dB apart by peak but
# 0.06-0.24 dB apart by in-band RMS.
REPEAT_LEVEL_TOLERANCE_DB = 0.3

# Floor on `SegmentLocation.confidence` below which a located sweep is not
# evidence (~0.03 fits a confident-looking multi-thousand-sample step from
# noise). Duplicated from crossover_v2.capture_dispatch's own floor and
# pinned equal by tests/test_measurement_integrity_floor_contracts.py.
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3

# How many TIMES more present the winning anchor hypothesis's witness must
# be than its runner-up's before `_resolve_anchor` may call it RESOLVED. 50
# sits near the geometric centre of the measured gap between
# cannot-discriminate (1.07-12.4) and resolved (197+) anchors. PROVISIONAL.
ANCHOR_DISCRIMINATION_RATIO = 50.0

# Max residual, ms, between a located VERIFY summed sweep and its scheduled
# slot; beyond it the timeline was spliced, not drifted. Twin of
# crossover_v2.capture_dispatch's MEASURE constant; inherited from MEASURE
# evidence (glitched at -25..-28 ms vs clean at <=1.5 ms), not re-derived.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0

# Sentinel for "no located sweeps were trustworthy enough to fit a step",
# distinct from 0.0 ("confidently no step"); a str so it can't be mistaken
# for a vanishingly small step.
DISCONTINUITY_UNRESOLVED = "unresolved"

# --- VERIFY capture integrity ---
# Three states: VERIFY plays ONE mono summed sweep, so some checks are
# structurally inapplicable and report `not_evaluated`, never a pass.
INTEGRITY_PASS = "pass"
INTEGRITY_FAIL = "fail"
INTEGRITY_NOT_EVALUATED = "not_evaluated"

# Frame accounting, asked before anything about the signal (both read
# FrameLedger; independently caused losses need both checks).
INTEGRITY_CHECK_RENDER_GAP = "capture_render_gap"
INTEGRITY_CHECK_FRAME_LEDGER = "frame_ledger"
# The checks a single summed sweep CAN answer.
INTEGRITY_CHECK_SWEEP_HEARD = "summed_sweep_heard"
INTEGRITY_CHECK_SWEEP_SCHEDULE = "summed_sweep_schedule"
INTEGRITY_CHECK_CLIPPED_RUN = "clipped_run"
# The MEASURE-side checks it CANNOT (MEASURE counterpart: DriftEstimate.glitch_inputs).
INTEGRITY_CHECK_REPEAT_EPSILON = "repeat_epsilon"
INTEGRITY_CHECK_REPEAT_LEVEL = "repeat_level_agreement"
INTEGRITY_CHECK_WITHIN_ROLE_DESYNC = "within_role_desync"
INTEGRITY_CHECK_DISCONTINUITY_STEP = "discontinuity_step"

# Why each unevaluated check could not run, stored on the check itself.
_INTEGRITY_NO_REPEAT_PAIR = "verify plays one summed sweep: no repeat pair"
_INTEGRITY_STEP_NEEDS_MORE_SWEEPS = (
    "a step fit needs more located sweeps than a verify program has"
)
_INTEGRITY_NO_SUMMED_SWEEP = "no summed sweep located in this capture"
_INTEGRITY_NO_STIMULUS = "no stimulus segment located in this capture"
_INTEGRITY_NO_RENDER_REPORT = (
    "the capture page reported no render-block counters"
)
_INTEGRITY_NO_FRAME_COUNT = "the capture page declared no frame count"
_INTEGRITY_SWEEP_NOT_HEARD = (
    "the summed sweep was not confidently located, so its schedule residual "
    "is not evidence"
)

DEFAULT_ALIGN_SEARCH_MS = 2.0  # geometry prior bound on |relative delay|

# Gated local-peak snap radius, as a fraction of the crossover period at Fc
# (docs/historical/crossover-measurement-reproducibility-plan.md §10).
# period/6 = lambda/6 is the GPS integer-ambiguity lobe-selection budget
# (Teunissen): error <= lambda/6 picks the correct comb lobe with >=99.7%
# probability. PROVISIONAL.
GCC_SNAP_RADIUS_PERIODS = 1.0 / 6.0

# Alignment estimator status vocabulary.
ALIGNMENT_OK = "ok"
ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"

# --- Joint (polarity, delay) alignment selection ---
# One objective: ripple of the predicted summed blend, correlation as seed
# and tie-break. Search span: +/- one period at Fc (the comb-lobe ambiguity
# interval), derived from the priors' Fc.
ALIGNMENT_FLATNESS_SPAN_PERIODS = 1.0
ALIGNMENT_FLATNESS_STEP_US = 10.0
# Point-count cap for bounded CPU: a low Fc widens the step to fit rather
# than scaling count as 1/Fc. Inactive at Fc >= ~500 Hz.
ALIGNMENT_FLATNESS_MAX_STEPS = 200
# Flat-minimum regularization, dB: within this much of the global minimum,
# keep the SEED pair so an applied alignment does not wander between
# re-measurements. Deliberately a separate constant from
# RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB so neither silently retunes the other.
ALIGNMENT_FLAT_MINIMUM_EPSILON_DB = 0.25

#: What the candidate's (polarity, delay) pair IS, never why an alternative was rejected.
ALIGNMENT_COMMITTED_FLAT_SUM = "flat_sum_committed"
ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR = "declared_committed_after_low_snr"
#: The declared polarity at the delay the applied graph already carries.
ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR = "applied_alignment_held_after_low_snr"
#: A graph is applied but its inter-driver delay could not be read, so no delay is committed.
ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY = (
    "no_delay_committed_after_unreadable_apply"
)
#: The delay came from a host-validated explicit prescription
#: (:data:`MeasurementPriors.explicit_alignment_delay_us`); polarity is still the flat-sum answer.
ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION = "explicit_prescription_committed"
#: The same prescription on a capture the SNR verdict refused for alignment.
ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR = (
    "explicit_prescription_held_after_low_snr"
)
ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND = "seed_committed_no_scoring_band"
ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED = "seed_committed_alignment_refused"
ALIGNMENT_COMMITMENTS = frozenset({
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND,
    ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED,
})
#: Membership rule: the capture's alignment evidence was wholly untrusted, so
#: neither polarity nor anchor may be spoken for
#: (tests/test_crossover_envelope_v2.py pins this against the household copy).
ALIGNMENT_DECLARED_POLARITY_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
})
#: Commitments an explicit prescription produced; read by
#: crossover_v2.coordinator._round_measurements as the prescription's ``committed`` bit.
ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
})
#: Commitments where the flat-sum objective chose the POLARITY — necessary but
#: not sufficient for :attr:`AlignmentPairSelection.polarity_agrees_with_sum`,
#: which checks :attr:`~AlignmentPairSelection.polarity_pinned` first.
_FLAT_SUM_POLARITY_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
})
#: Commitments the selector itself made, vs. the two where the seed simply stood.
_SELECTOR_COMMITTED_OBJECTIVES = frozenset({
    *_FLAT_SUM_POLARITY_OBJECTIVES,
    *ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
})

#: Verdict at which a branch stops being evidence a polarity flip may rest on
#: (the ALIGNMENT decision class, 35 dB ``DRIVER.alignment_snr_ok_db``, no
#: ``reduced`` rung); ``unknown``/absent means never computed.
ALIGNMENT_SNR_REFUSAL_VERDICT = "insufficient"

#: Where :func:`_driver_snr_block` files its ALIGNMENT-class verdict; read by :func:`driver_alignment_snr_verdict`.
DRIVER_SNR_ALIGNMENT_KEY = "alignment"

# Ripple-optimal trim POLISH: re-solve the tweeter trim for minimum ripple
# over the seed +/- this window. A jts3 corpus (5 runs) observed a
# 1.7-6.3 dB gap between seed and ripple optimum, so +/-10 dB is never
# truncated at its own edge.
RIPPLE_TRIM_SEARCH_WINDOW_DB = 10.0
RIPPLE_TRIM_SEARCH_STEP_DB = 0.1

# Flat-minimum regularization, dB: within this much of the scan's global
# minimum, prefer whichever is closest to the seed (above the 0.1 dB grid
# step so a sharp minimum isn't widened into a plateau by quantization).
RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB = 0.25

# A trim is a passive level-match: never net gain, never beyond the shared
# -60 dB floor (jasper.active_speaker.level_trim.MAX_ATTENUATION_DB,
# mirrored locally — this module does not import jasper.active_speaker).
RIPPLE_TRIM_MAX_DB = 0.0
RIPPLE_TRIM_MIN_DB = -60.0

# How far the two branches' realized levels (mirrored +/-1-octave
# half-bands about Fc) may sit apart after the committed trim before being
# REPORTED as mislevelled — a DISCLOSURE, not a gate
# (crossover_v2.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB). Floor: five
# archived JTS3 cdhorn captures agree to 0.51-1.30 dB. Ceiling: an
# inter-branch error reaches flat_spec.SPEC_BANDS[1]'s 2.0 dB tolerance at
# ~2.0 dB, so 2-3 dB errors are spec failures this does not flag.
REALIZED_LEVEL_MATCH_TOLERANCE_DB = 3.0

# Direct-arrival window, ms, isolating each driver's IR before deconvolution
# magnitude / alignment (mirrors deconv defaults).
IR_PRE_MS = 5.0
IR_POST_MS = 60.0

# Deconvolution window pre-guard, s, before the scheduled sweep position;
# shared by both drivers so their IR peaks land pre-guard sample +/- delay.
DECONV_PRE_GUARD_S = 0.25

# Gain solve: land the MEASURE capture peak in [-12, -9] dBFS with >=6 dB
# guard. A CEILING rather than a target — see `_solve_role_gain`.
DEFAULT_TARGET_CAPTURE_DBFS = -10.5
GAIN_GUARD_DB = 6.0
GAIN_MAX_DIGITAL_PEAK_DBFS = -GAIN_GUARD_DB  # digital peak must sit ≤ this

# --- SNR-solved MEASURE level ---
# MEASURE is the only phase whose level is solved (others ride
# program.BASE_STIMULUS_PEAK_DBFS clamped by the driver cap); the room's
# noise floor decides the fit, from CHECK's own measurement.
# This margin is the solve's own insurance on top of a band's SNR
# requirement; ambient evidence is CHECK's 12 s window.
MEASURE_SNR_SOLVE_MARGIN_DB = 6.0

# Peak-to-RMS of the excitation, dB: every stimulus is a constant-amplitude
# synchronized swept sine, so peak sits 10*log10(2) dB above full-band RMS.
# Measured at 3.02-3.03 dB on both real MEASURE sweeps.
SWEEP_PEAK_TO_RMS_DB = 3.0103


def sweep_band_crest_factor_db(
    sweep_hz: tuple[float, float], band_hz: tuple[float, float]
) -> float:
    """dB from a swept sine's PEAK down to its RMS inside ``band_hz``.

    Converts the MEASURE level solve's capture-PEAK budget into the
    ambient-band-RMS terms its SNR demand is stated in. Two terms, exact
    for the constant-amplitude exponential sweep
    ``program.segment_stimulus`` renders: :data:`SWEEP_PEAK_TO_RMS_DB`
    (peak to full-band RMS) plus ``10*log10(ln(f2/f1) / ln(hi/lo))`` (band
    occupancy — the sweep dwells in ``[lo, hi]`` for that fraction of its
    duration, and dilution does not cancel after deconvolution).
    ``band_hz`` is clipped to ``sweep_hz`` first.

    Validated against the rendered stimulus to within 0.03 dB on both
    production MEASURE sweeps. Erring LOUD: an ambient row wider than the
    sweep's slice of it (`snr_policy.CROSSOVER_SNR_BANDS_HZ`) over-states
    the demand, keeping MEASURE louder — the safe direction, same as
    :func:`_solve_role_gain`'s row-width coarseness.
    """
    lo = max(float(band_hz[0]), float(sweep_hz[0]))
    hi = min(float(band_hz[1]), float(sweep_hz[1]))
    f1, f2 = float(sweep_hz[0]), float(sweep_hz[1])
    if not (hi > lo > 0.0 and f2 > f1 > 0.0):
        # No overlap, or a degenerate band — no occupancy term to compute.
        # `_ambient_rows_in_band` only yields overlapping rows, so this is
        # defense against a malformed band, not a live path.
        return SWEEP_PEAK_TO_RMS_DB
    return SWEEP_PEAK_TO_RMS_DB + 10.0 * math.log10(
        math.log(f2 / f1) / math.log(hi / lo)
    )


# Vocabulary for `RoleGainSolve.bound_by` — which limit chose the level.
GAIN_BOUND_FLAT_TARGET = "flat_target"          # room too noisy to back off
GAIN_BOUND_ROOM_SNR = "room_snr"                # the fit's own SNR need
GAIN_BOUND_PILOT_SNR = "pilot_snr"              # the quiet pilot's SNR guard
GAIN_BOUND_CAPTURE_FLOOR = "capture_floor"      # `DRIVER.peak_too_low_dbfs`
GAIN_BOUND_NO_AMBIENT_EVIDENCE = "no_ambient_evidence"  # disclosed fallback
# Every SNR arm resolved below `DRIVER.peak_too_low_dbfs`: a disclosed
# refusal to solve, never a shipped level — see `_solve_role_gain`.
GAIN_BOUND_DEGENERATE_AMBIENT = "degenerate_ambient"
GAIN_BOUNDS = frozenset({
    GAIN_BOUND_FLAT_TARGET,
    GAIN_BOUND_ROOM_SNR,
    GAIN_BOUND_PILOT_SNR,
    GAIN_BOUND_CAPTURE_FLOOR,
    GAIN_BOUND_NO_AMBIENT_EVIDENCE,
    GAIN_BOUND_DEGENERATE_AMBIENT,
})

# Behavioral linearity tolerance, dB (design §3.4). Measured band-relative +
# ambient-compensated (`_pilot_observations`), never full-band peak: LF room
# rumble otherwise inflates the quiet pilot's peak and compresses the delta.
LINEARITY_TOLERANCE_DB = 0.5

# Pilot edge-fade trim, s: `sweep.synchronized_swept_sine` applies a fixed
# 5 ms fade-in/out to every stimulus; trimming that span before measuring
# level keeps the RMS estimate to the pilot's steady-state portion.
PILOT_FADE_TRIM_S = 0.005

# Low-SNR honest routing (`_pilot_observations`): ambient power subtraction
# only removes the room's noise-floor bias when the quiet pilot's own
# in-band power clears ambient power by enough margin that residual bias
# from ambient nonstationarity stays a small fraction of
# `LINEARITY_TOLERANCE_DB`. Solving for the minimum trustworthy in-band SNR
# gives `PILOT_MIN_SNR_DB` ≈ 12.4 dB; real jts3 hardware measures ≈26-30 dB,
# so this floor exists for the genuinely marginal case, not the common one.
AMBIENT_NONSTATIONARITY_DB = 3.0
LINEARITY_SNR_BIAS_BUDGET_FRACTION = 0.5
_pilot_snr_k = 10.0 ** (AMBIENT_NONSTATIONARITY_DB / 10.0)
_pilot_snr_linear_min = (10.0 / math.log(10.0)) * (_pilot_snr_k - 1.0) / (
    LINEARITY_TOLERANCE_DB * LINEARITY_SNR_BIAS_BUDGET_FRACTION
)
PILOT_MIN_SNR_DB = 10.0 * math.log10(_pilot_snr_linear_min)

# Channel-map discriminator TARGET rise, dB (`_channel_map_ok`): an absolute
# floor — a driver whose declared band never rose over the room did not play.
# Derived from a jts3 hardware table (woofer +22-30 dB, tweeter +27 dB).
CHANNEL_MAP_TARGET_RISE_DB = 12.0

# The CROSS test is a RATIO, not an additive bound: an additive bound is
# tuned against one frame's room floor and a healthy speaker fails it at a
# louder session level (measured on hardware: target/cross rise tracks a
# roughly fixed relative level, not an absolute one, across a 10.5 dB level
# span). This half guards abnormal cross-band energy (bleed, skirt,
# nonlinearity) — mis-wiring is caught by the TARGET floor above, not this
# ratio (seven wiring shapes moved cross rise by only +/-0.4 dB). 12.0 gives
# >=32 dB margin under the hardware table while still refusing the
# degenerate both-bands case. PROVISIONAL.
CHANNEL_MAP_MIN_ISOLATION_DB = 12.0

# The ratio is judged only once target_rise clears FLOOR + this bound (a
# quiet-but-correct capture below that is not a meaningful ratio and falls
# through to `snr_floor`/the TARGET floor instead of a `channel_map_mismatch`
# hard stop).
CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB = (
    CHANNEL_MAP_TARGET_RISE_DB + CHANNEL_MAP_MIN_ISOLATION_DB
)

# VERIFY tracking-error smoothing: 1/6-octave (design §5.2).
VERIFY_TRACKING_SMOOTHING_FRACTION = 6

# VERIFY tracking MAX comparator: a bin is excluded when the predicted sum
# sits more than this many dB below its own median over the tracking band —
# inside a predicted notch, depth agreement is hypersensitive and not a
# meaningful tracking signal (one hardware failure at 3.05 dB rms / 27.83 dB
# max was a shifted predicted notch, not a broadband divergence). RMS stays
# full-band — see `_analyze_verify`.
VERIFY_NOTCH_EXCLUSION_DB = 12.0

# Three flatness claims, fixed ownership: "is the speaker flat?" is
# jasper.active_speaker.flat_spec's cloud gauge; "did THIS crossover track
# its prediction?" is `verify_tracking` below; "did THIS crossover hand off
# as designed?" is `verify_absolute` below. The cloud gauge cannot own the
# last two: it is assembled after `_verify_verdict` runs, some paths have no
# post-apply cloud at all, and its spatial mean/self-reference answer a
# different question. `CrossoverCandidate.flatness_improvement_db` is an
# unrelated Layer-1b metric, not a spec claim.

ANALYSIS_KIND = "jts_program_analysis"


@dataclass(frozen=True)
class MeasurementGeometry:
    """Declared physical geometry the analysis corrects for.

    ``mic_distance_m`` is the prescribed on-axis mic distance (~1 m, design
    §5.2); ``driver_spacing_m`` is the declared woofer↔tweeter spacing. Their
    deterministic parallax ``(√(r²+d²)−r)/c`` is subtracted from the measured
    delay so what remains is the electrical branch delay to apply.
    """

    driver_spacing_m: float = 0.0
    mic_distance_m: float = 1.0
    speed_of_sound_m_s: float = DEFAULT_SOUND_SPEED_M_S

    def parallax_us(self) -> float:
        """The deterministic mic-parallax term, in µs.

        Aim assumption (design §5.2): the mic sits on the tweeter axis at
        distance ``r``, so the woofer's off-axis excess ``sqrt(r^2+d^2) - r``
        inflates the measured arrival difference; subtracting it leaves the
        electrical branch delay.
        """
        r = float(self.mic_distance_m)
        d = float(self.driver_spacing_m)
        c = float(self.speed_of_sound_m_s)
        if r <= 0 or c <= 0 or d <= 0:
            return 0.0
        extra_m = math.sqrt(r * r + d * d) - r
        return extra_m / c * 1e6


@dataclass(frozen=True)
class AppliedAlignment:
    """What the APPLIED graph says about its own inter-driver delay.

    ``delay_us`` is that delay in :class:`AlignmentEstimate`'s signed frame,
    or ``None`` when a graph is applied but its record does not say. A
    wrapper rather than a bare ``float | None``: the question has THREE
    answers — absent (``MeasurementPriors.applied_alignment is None``) means
    nothing is commissioned, while ``AppliedAlignment(None)`` means
    something IS playing and we cannot say what.
    """

    delay_us: float | None


@dataclass(frozen=True)
class MeasurementPriors:
    """Per-analysis priors the program itself does not carry.

    ``crossover_fc_hz`` scopes the overlap band and the VERIFY window;
    ``align_search_ms`` bounds the delay search; ``target_capture_dbfs`` is
    the MEASURE capture-peak target the CHECK gain solve aims for.
    ``predicted_sum`` is the MEASURE-predicted summed magnitude VERIFY
    compares against, overridden with a LINEARIZED-branch prediction
    whenever Layer-1a linearization was fitted (an un-linearized prediction
    reads a deterministic ~1.7 dB mismatch against a ±1.5 dB tolerance).

    ``measure_excited_band_hz`` is the band every MEASURE branch actually
    swept, forwarded so VERIFY's tracking comparison trusts the same band
    ``predicted_sum`` was built in; ``None`` falls back to the unclamped
    band. ``alignment_delay_bounds_us`` is the declaration-derived
    applied-delay magnitude range the flatness refinement may search;
    ``None`` keeps GCC as the applied-delay estimate.

    ``applied_alignment`` and ``explicit_alignment_delay_us`` are host facts
    read on exactly one path, :func:`_select_alignment_pair`.
    ``applied_alignment`` (what this speaker already plays) is committed
    only on the low-SNR refusal — never a seed, bound, or prior on the
    scored path, since a capture good enough to score must not be pulled
    toward the answer the speaker already has. ``explicit_alignment_delay_us``
    is a host-validated prescription (:func:`jasper.active_speaker.crossover_v2.alignment_prescription.read_alignment_prescription`),
    committed as the delay; ``None`` leaves automatic selection
    byte-identical. ``explicit_alignment_polarity_sign`` pins the BASIN a fit
    may solve in (delay and polarity are degenerate on axis); never set
    without ``explicit_alignment_delay_us``.

    ``mic_tier`` is the correction-envelope trust tier
    (``linearization_envelope.MIC_TIERS``); ``None`` means "no tier known",
    which the v2 session's Layer-1a gate treats as ineligible, never a guess.
    """

    crossover_fc_hz: float | None = None
    align_search_ms: float = DEFAULT_ALIGN_SEARCH_MS
    target_capture_dbfs: float = DEFAULT_TARGET_CAPTURE_DBFS
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    ambient_report: Mapping[str, Any] | None = None
    measure_excited_band_hz: tuple[float, float] | None = None
    alignment_delay_bounds_us: tuple[float, float] | None = None
    applied_alignment: AppliedAlignment | None = None
    explicit_alignment_delay_us: float | None = None
    explicit_alignment_polarity_sign: int | None = None
    mic_tier: str | None = None
    # Host-evaluated transfers: the kernel may not import jasper.active_speaker.
    measurement_protection_response_by_role: Mapping[
        str, Callable[[np.ndarray], np.ndarray]
    ] | None = None
    configured_crossover_response_by_role: Mapping[
        str, Callable[[np.ndarray], np.ndarray]
    ] | None = None
    configured_polarity_sign_by_role: Mapping[str, int] | None = None
    # §4.2's candidate-required bins per role (host-owned).
    candidate_required_band_hz_by_role: Mapping[
        str, tuple[float, float]
    ] | None = None
    # Whether a mic calibration curve was applied; ``None`` means "not
    # resolved either way". See ``ProgramAnalysis.mic_calibrated``.
    mic_calibrated: bool | None = None


@dataclass(frozen=True)
class SegmentLocation:
    """Where one program segment landed in the capture, and its integrity."""

    segment_id: str
    kind: str
    role: str | None
    scheduled_start: int
    located_start: int
    residual_samples: float
    confidence: float
    peak_dbfs: float
    clipped: bool


@dataclass(frozen=True)
class IntegrityCheck:
    """One capture-integrity question, and what this capture said about it.

    ``status`` is one of :data:`INTEGRITY_PASS`/:data:`INTEGRITY_FAIL`/
    :data:`INTEGRITY_NOT_EVALUATED`; ``reason`` is filled only on
    ``not_evaluated``, so "did not run" and "ran and passed" stay distinct.
    """

    name: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class CaptureIntegrity:
    """What a VERIFY capture's own timeline says about whether it is usable.

    The evidence fields (``locate_confidence_min``,
    ``schedule_residual_ms_worst``, ``clipped_segments``) are reported
    whether or not the check drawn from it ran, so a reader never infers a
    splice from a number that is really just noise. ``failed``/
    ``not_evaluated``/``glitched`` derive from ``checks`` rather than being
    stored. ``checks`` is ordered most-fundamental-first (a sweep nobody
    heard explains its own residual). ``None`` on :class:`ProgramAnalysis`
    means "no evidence", never "clean".
    """

    checks: tuple[IntegrityCheck, ...] = ()
    locate_confidence_min: float | None = None
    # SIGNED: positive means the sweep arrived LATE (the insertion shape).
    schedule_residual_ms_worst: float | None = None
    clipped_segments: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.status == INTEGRITY_FAIL)

    @property
    def not_evaluated(self) -> tuple[str, ...]:
        return tuple(
            c.name for c in self.checks if c.status == INTEGRITY_NOT_EVALUATED
        )

    @property
    def glitched(self) -> bool:
        """True when at least one EVALUATED check failed.

        A record of nothing but ``not_evaluated`` is not glitched — it is
        unexamined, and the ``not_evaluated`` list is how a reader tells the
        two apart.
        """
        return bool(self.failed)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record for a verdict payload / durable evidence."""
        return {
            "glitched": self.glitched,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    # Present only where there IS one, so a consumer cannot
                    # read an empty string as a stated reason.
                    **({"reason": c.reason} if c.reason else {}),
                }
                for c in self.checks
            ],
            "locate_confidence_min": self.locate_confidence_min,
            "schedule_residual_ms_worst": self.schedule_residual_ms_worst,
            "clipped_segments": list(self.clipped_segments),
        }


@dataclass(frozen=True)
class DriftEstimate:
    """In-capture clock-drift estimate + the glitch verdict (design §5.6.3).

    ``repeat_level_delta_db`` is the woofer-repeat in-band-RMS level
    disagreement, one of the three glitch inputs. ``per_role_epsilon_ppm``
    is diagnostic only (never gated — only the woofer pair decides
    ``glitch_detected``), empty for a role with <2 occurrences.
    ``glitch_inputs`` names which of the four bounds tripped
    (``epsilon_out_of_bound``/``residual_desync``/``repeat_level_disagree``/
    ``timeline_slip``), empty on a clean capture.

    ``discontinuity_samples``/``discontinuity_after_segment`` describe a
    single timeline step: signed size in samples (positive => LATE) and the
    segment id it landed after. ``0.0``/``""`` when no step is resolved on a
    confidently-located capture; ``DISCONTINUITY_UNRESOLVED`` (a str)
    instead when a located sweep fell below
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` — a fabricated step gets a distinct
    sentinel rather than a silent ``0.0``. Populate exactly when
    ``timeline_slip`` fires; read the MAGNITUDE only (sign/segment ambiguous
    at an even schedule index).
    """

    epsilon_ppm: float
    max_residual_samples: float
    glitch_detected: bool
    repeat_level_delta_db: float = 0.0
    per_role_epsilon_ppm: Mapping[str, float] = field(default_factory=dict)
    glitch_inputs: tuple[str, ...] = ()
    discontinuity_samples: float | str = 0.0
    discontinuity_after_segment: str = ""


@dataclass(frozen=True)
class DriverResponse:
    """One driver's gated complex response, calibrated if a cal was supplied.

    ``repeat_responses`` holds this same driver's additional located sweep
    occurrences (``repeat_index`` 1, 2, ...), populated only on the PRIMARY
    response (``sweep_w``/``sweep_t``). Diagnostic only — nothing here feeds
    the candidate/trim/alignment math, which stays anchored to the primary.
    """

    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    complex_tf: np.ndarray
    gating: dict[str, Any]
    snr: dict[str, Any] | None
    validity_floor_hz: float | None
    repeat_responses: tuple["DriverResponse", ...] = ()
    repeat_index: int | None = None


@dataclass(frozen=True)
class AlignmentEstimate:
    """Tweeter-vs-woofer relative delay + polarity (design §5.6.5).

    Sign convention: ``delay_us`` is ``(D_woofer - D_tweeter)`` after
    parallax removal — POSITIVE means the tweeter's acoustic arrival is
    EARLIER and its branch must be delayed by that amount.

    GCC is the capture-quality seed: ``seed_delay_us`` records that
    corrected delay, while ``delay_us`` is whatever
    :func:`_select_alignment_pair` COMMITTED (the flattest-summing grid
    point, or a delay the refused-SNR path never supplied).
    ``confidence_source='gcc_phat_seed'`` marks that ``confidence`` belongs
    to the seed, not the commitment. ``raw_delay_us`` is the pre-parallax
    coordinate, so ``delay_us == raw_delay_us - parallax_us``.

    ``anchor_delay_us`` is the drift-corrected physical peak-gap anchor;
    ``snapped_delay_us`` is that anchor snapped to the nearest local GCC-PHAT
    maximum within +/-(period/6) at Fc (:data:`GCC_SNAP_RADIUS_PERIODS`),
    ``None`` when no local maximum exists in range or the seed was refused.

    ``status`` is :data:`ALIGNMENT_OK` for a trustworthy estimate; when the
    correlation peak lands at the +/-search-window edge (a likely clamped
    artifact), ``status`` is :data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW`
    and ``confidence`` is forced to 0.0 — callers must not apply ``delay_us``
    then.

    Polarity follows the same seed-then-selection shape as the delay.
    ``polarity_agrees_with_sum`` is ``True``/``False`` once a flat-sum
    selection has answered it, ``None`` on a fresh/hand-built estimate; a
    disagreement is ordinary operation, recorded rather than raised.
    """

    delay_us: float
    raw_delay_us: float
    parallax_us: float
    polarity: str  # "normal" | "inverted"
    polarity_sign: int  # +1 | -1
    confidence: float
    status: str = ALIGNMENT_OK
    seed_delay_us: float | None = None
    confidence_source: str = "gcc_phat"
    anchor_delay_us: float | None = None
    snapped_delay_us: float | None = None
    polarity_agrees_with_sum: bool | None = None


@dataclass(frozen=True)
class CrossoverCandidate:
    """The proposed measured candidate (design §5.6.6).

    The (polarity, delay) pair is chosen jointly on predicted summed blend
    flatness: :func:`_select_alignment_pair` scores both polarities across a
    delay grid and commits the flattest pair, while correlation supplies the
    SEED pair and tie-break. ``alignment_objective`` names what was
    committed (:data:`ALIGNMENT_COMMITMENTS`); ``seed_polarity_sign`` is
    correlation's own answer, so a disagreement is readable off the
    candidate. ``left_anchor_lobe`` records the committed delay sitting
    outside the anchor's comb lobe — carried here (not just the journal)
    because a wrong-lobe commitment is magnitude-flat and time-wrong, so an
    on-axis VERIFY cannot see it.

    ``anchor_delay_us`` is the bare anchor; ``snap_delta_us`` is
    ``committed - anchor``; ``snap_found`` records whether a local
    correlation peak existed inside the snap radius.
    ``alignment_seed_ripple_db`` is ripple at the SEED pair,
    ``flatness_improvement_db`` is ``seed_ripple - committed_ripple``:
    non-negative on the flat-sum path by construction, but can be NEGATIVE
    on the low-SNR path — the disclosure of what declining a
    noise-derived flatness claim cost on paper.

    ``predicted_ripple_db`` is measured on the INDEPENDENTLY ALIGNED
    (zero-residual) branch sum at the committed polarity — deliberately not
    the committed-delay model ``ProgramAnalysis.predicted_sum`` uses, since
    it asks a capture-quality question (how coherently these branches sum at
    all) that feeds ``crossover_v2_flow``'s disclosure-only
    ``MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB``.

    ``trim_db`` is the APPLIED trim (ripple-optimal where trusted, otherwise
    the band-average fallback); ``trim_band_average_db`` preserves
    ``solve_branch_trims``'s own seed result for forensics. A persisted
    candidate is evidence, not config, and is never migrated across a frame
    change.
    """

    trim_db: Mapping[str, float]
    polarity: str
    delay_us: float
    predicted_ripple_db: float
    confidence: float
    alignment_seed_ripple_db: float | None = None
    flatness_improvement_db: float | None = None
    anchor_delay_us: float | None = None
    snap_delta_us: float | None = None
    snap_found: bool = False
    trim_band_average_db: Mapping[str, float] | None = None
    alignment_objective: str = ""
    seed_polarity_sign: int | None = None
    left_anchor_lobe: bool = False
    #: Did correlation's polarity answer survive the objective that chose it?
    #: Carried from :attr:`AlignmentPairSelection.polarity_agrees_with_sum`.
    #: ``None`` means no flat sum ever answered it.
    polarity_agrees_with_sum: bool | None = None
    #: Did the REQUEST hold the polarity axis, rather than a measurement?
    #: Carried from :attr:`AlignmentPairSelection.polarity_pinned` — not
    #: derivable from the other two fields here.
    polarity_pinned: bool = False
    #: The ripple polish's SIGNED trim excursion, dB, when REJECTED; ``None``
    #: when nothing was thrown away. Distinguishes a rejected polish from an
    #: admitted no-op and a one-sided skip (all three share
    #: ``trim_db[tweeter] == trim_band_average_db[tweeter]``).
    ripple_polish_rejected_delta_db: float | None = None


@dataclass(frozen=True)
class PilotObservation:
    """One driver's CHECK pilot pair — level, linearity, channel-map sanity.

    ``level_lo_dbfs``/``level_hi_dbfs`` are band-relative and
    ambient-compensated; they feed ONLY the linearity verdict
    (``captured_delta_db``'s ambient-subtraction bias cancels between the
    two levels) and must never feed an ABSOLUTE-level consumer — ambient
    subtraction moved `_solve_gain_plan`'s captured-peak target 13-17 dB
    hotter when tried. ``peak_lo_dbfs``/``peak_hi_dbfs`` are the dedicated
    NON-ambient-subtracted levels `_solve_gain_plan` reads instead.

    ``snr_valid`` is True when the quiet pilot's in-band SNR clears
    `PILOT_MIN_SNR_DB`; when False, ``linearity_ok`` is ``None`` (unknown,
    never forced True or False) so an untrustworthy estimate is never
    mistaken for a linearity failure or pass. Defaults True. ``snr_db`` is
    the underlying estimate, ``+inf`` with no ambient window to validate
    against.

    ``channel_map_ok`` is tri-state like ``linearity_ok``: ``None`` means
    no evidence, never a pass. ``channel_map_target_rise_db``/
    ``channel_map_cross_rise_db`` are the raw rise numbers
    `_channel_map_ok` computed on the way to the verdict; ``None`` on the
    fallback total-energy-fraction path (v2 MEASURE/VERIFY) or with no
    other role to compare against — published raw since the isolation
    ratio is derivable from them but not the reverse.

    ``programmed_hi_gain_db`` is the HI segment's declared ``gain_db``, so a
    caller can compute the capture chain's own transfer without binding
    back to the source program; ``None`` predates this field and must be
    treated as "nothing to compare", never ``0.0``.
    """

    role: str
    level_lo_dbfs: float
    level_hi_dbfs: float
    programmed_delta_db: float
    captured_delta_db: float
    linearity_ok: bool | None
    channel_map_ok: bool | None
    snr_valid: bool = True
    peak_lo_dbfs: float = DBFS_FLOOR
    peak_hi_dbfs: float = DBFS_FLOOR
    snr_db: float = math.inf
    channel_map_target_rise_db: float | None = None
    channel_map_cross_rise_db: float | None = None
    programmed_hi_gain_db: float | None = None


@dataclass(frozen=True)
class RoleGainSolve:
    """One driver's MEASURE level solve, and the evidence it rests on.

    ``gain_db`` is the digital gain the MEASURE composer will actually schedule
    for this role; ``flat_target_gain_db`` is the level-only figure (land the
    capture peak on ``MeasurementPriors.target_capture_dbfs``, clamped by the
    ≥6 dB digital guard). The solve never exceeds that flat figure, so the pair
    is also the disclosure of how much quieter this session's MEASURE got.

    ``bound_by`` names which limit chose the number — one of the
    ``GAIN_BOUND_*`` constants. It is the honesty field: a
    ``GAIN_BOUND_NO_AMBIENT_EVIDENCE`` solve is the disclosed fallback to the
    flat target, never a silent guess, and ``GAIN_BOUND_FLAT_TARGET`` means the
    room was noisy enough that the SNR requirement wanted at least the flat
    level, so nothing moved.

    ``ambient_dbfs`` / ``required_snr_db`` / ``crest_factor_db`` /
    ``required_capture_dbfs`` are the ROOM-SNR demand and only that: the worst
    overlapping ambient band's level, the SNR this solve demanded above it, the
    stimulus crest factor that converts that band-RMS demand into the
    capture-PEAK units everything else here uses (see
    :func:`sweep_band_crest_factor_db`), and their sum. A coherent quadruple —
    the last is the first three added — and they stay the room demand even when
    a DIFFERENT arm won, so on ``GAIN_BOUND_PILOT_SNR`` or
    ``GAIN_BOUND_DEGENERATE_AMBIENT`` ``required_capture_dbfs`` is the room
    demand that arm overrode, not the capture peak aimed at. All four are
    ``None`` on the no-evidence fallback — a missing number is never a zero —
    and ``crest_factor_db`` is additionally ``None`` on a solve persisted
    before the demand carried a crest term at all.
    """

    role: str
    gain_db: float
    flat_target_gain_db: float
    bound_by: str
    band_hz: tuple[float, float] | None = None
    ambient_dbfs: float | None = None
    required_snr_db: float | None = None
    required_capture_dbfs: float | None = None
    crest_factor_db: float | None = None

    @property
    def reduction_db(self) -> float:
        """How much quieter than the flat target this solve is (≥ 0)."""
        return self.flat_target_gain_db - self.gain_db

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "gain_db": round(self.gain_db, 3),
            "flat_target_gain_db": round(self.flat_target_gain_db, 3),
            "reduction_db": round(self.reduction_db, 3),
            "bound_by": self.bound_by,
            "band_hz": (
                [round(self.band_hz[0], 1), round(self.band_hz[1], 1)]
                if self.band_hz is not None else None
            ),
            "ambient_dbfs": (
                round(self.ambient_dbfs, 2) if self.ambient_dbfs is not None else None
            ),
            "required_snr_db": (
                round(self.required_snr_db, 2)
                if self.required_snr_db is not None else None
            ),
            "required_capture_dbfs": (
                round(self.required_capture_dbfs, 2)
                if self.required_capture_dbfs is not None else None
            ),
            "crest_factor_db": (
                round(self.crest_factor_db, 2)
                if self.crest_factor_db is not None else None
            ),
        }


@dataclass(frozen=True)
class GainPlan:
    """Solved MEASURE digital gains (design §5.2).

    ``role_solves`` carries the per-role derivation behind ``gain_db`` — see
    :class:`RoleGainSolve`. Empty for a construction site that predates the
    field; a consumer must read that as "no derivation published", never as "no
    reduction happened".
    """

    gain_db: Mapping[str, float]
    predicted_peak_dbfs: float
    snr_floor_ok: bool
    role_solves: Mapping[str, RoleGainSolve] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgramAnalysis:
    """The deterministic result of one ``(program, capture)`` pair."""

    phase: str
    program_id: str
    locations: tuple[SegmentLocation, ...]
    drift: DriftEstimate | None = None
    driver_responses: tuple[DriverResponse, ...] = ()
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    ambient_report: dict[str, Any] | None = None
    # Passthrough of MeasurementPriors.mic_tier; set only by _analyze_measure.
    mic_tier: str | None = None
    # True when §4.2's `M*C/P` composition ran, so these responses carry the
    # crossover shoulders the fitter's branch-input invariant assumes.
    configured_path_composed: bool = False
    pilots: tuple[PilotObservation, ...] = ()
    linearity_ok: bool | None = None
    channel_map_ok: bool | None = None
    # Aggregate of ``PilotObservation.snr_valid`` (``all(...)``); ``None``
    # with no pilots. False routes to `REASON_SNR_FLOOR`/
    # `REASON_PILOT_LEVEL_COLLAPSE`, never `REASON_AGC_BEHAVIORAL_FAIL`.
    pilot_snr_ok: bool | None = None
    gain_plan: GainPlan | None = None
    summed_response: DriverResponse | None = None
    summed_ripple_db: float | None = None
    # Notch depth at Fc below the shoulders on THIS capture's summed curve.
    reverse_null_depth_db: float | None = None
    # Measured-vs-predicted scalars for one VERIFY capture, plus the "frame"
    # the curves were compared across (tilt disclosed rather than corrected
    # for — see :func:`_analyze_verify`'s frame-discipline block).
    verify_tracking: dict[str, Any] | None = None
    # The ABSOLUTE crossover-region result for one VERIFY capture — see
    # `_verify_absolute_result`; own field since it has a different
    # reference/presence condition than `verify_tracking`. Always a dict on
    # a VERIFY analysis (numbers or ``{"not_evaluated": <reason>}``); ``None`` elsewhere.
    verify_absolute: dict[str, Any] | None = None
    # Why a MEASURE analysis carries no ``alignment``/``candidate``; ``None``
    # on every analysis that has them.
    measure_pair_not_evaluated: str | None = None
    # The SMOOTHED (freqs_hz, measured_db, predicted_db) triple the tracking
    # scalars were reduced from — own field since `verify_tracking` travels
    # to the phone and these are full curves. Feeds the delta probe
    # (jasper.active_speaker.delta_probe) so it grades the same comparison
    # the tracking gate does. ``None`` whenever `verify_tracking` is.
    verify_tracking_curve: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    # MEASURE-predicted summed magnitude at the candidate's COMMITTED trim
    # and delay, handed to VERIFY as `MeasurementPriors.predicted_sum` so
    # VERIFY's pass is |measured - predicted| <= +/-1.5 dB (design §5.2).
    # Quality is graded separately: `crossover_v2_flow.spec_report_for_predicted_sum`
    # and `CrossoverCandidate.predicted_ripple_db`.
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    # Set by MEASURE from `drift.glitch_detected`, by VERIFY from
    # `capture_integrity.glitched` — a one-bit projection of the record that owns the fact.
    glitch_detected: bool = False
    # The per-check VERIFY capture-integrity record `glitch_detected`
    # summarizes. Set by `_analyze_verify` on every VERIFY-phase analysis
    # (incl. spatial-cloud positions). ``None`` on CHECK/MEASURE means "no
    # evidence", never "clean" — see :class:`CaptureIntegrity`.
    capture_integrity: CaptureIntegrity | None = None
    # End-to-end frame accounting, set on EVERY phase by
    # `analyze_program_capture` (unlike `capture_integrity`, which only
    # VERIFY turns into graded checks). ``None`` only on analyses built
    # directly rather than through `analyze_program_capture`.
    frame_ledger: FrameLedger | None = None
    # True when `_resolve_anchor` could not tell this capture's competing
    # timeline interpretations apart, so no number below reliably attributes
    # energy to a driver. Set on every phase; only CHECK reads it (its
    # pilots re-locate onto each other under the shift, producing a
    # confident-looking wiring verdict instead of an honest "not found").
    # ``False`` (not ``None``) default: unambiguous by construction.
    anchor_ambiguous: bool = False
    # Passthrough of MeasurementPriors.mic_calibrated, set at the same site
    # as `mic_tier`. ``None`` means "not resolved either way", never a guess.
    mic_calibrated: bool | None = None
