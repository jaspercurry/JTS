# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of a crossover excitation-program capture (design §5.6).

``analyze_program_capture(program, samples, sample_rate) -> ProgramAnalysis``
derives segment locations, per-segment integrity, in-capture clock drift,
per-driver gated responses, tweeter-vs-woofer alignment and the crossover
candidate from the ``(program, capture)`` pair alone. No I/O, no product
policy, and no ``jasper.correction`` / ``jasper.active_speaker`` import
(``tests/test_correction_boundary_ssot.py`` pins that boundary), so product
crossover transfers arrive as host-evaluated per-role callables on
:class:`MeasurementPriors`.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import analysis as analysis_mod
from jasper.audio_measurement import calibration as calibration_mod
from jasper.audio_measurement import deconv, gate_disclosure, gating, snr_policy
from jasper.audio_measurement.frame_fit import FrameComparison, fit_frame
from jasper.audio_measurement.frame_ledger import FrameLedger, reconcile_capture_frames
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_PILOT,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    PROGRAM_PHASE_CHECK,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_PHASE_VERIFY,
    STIMULUS_KINDS,
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.timeline_slip import (
    GLITCH_INPUT_TIMELINE_SLIP,
    TimelineStepFit,
    fit_timeline_step,
    slip_rejects_capture,
)
from jasper.audio_measurement.alignment import (
    GCC_UPSAMPLE,
    _bandlimit,
    _gcc_local_peak_snap,
    cross_correlation_alignment,
    gcc_phat,
    parabolic_peak,
)
from jasper.audio_measurement.comparison_bands import (
    OVERLAP_OCTAVE_RATIO,
    branch_snr_band_hz,
    crossover_region_band_hz,
    overlap_band_hz,
    verify_tracking_band_hz,
)
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

logger = logging.getLogger(__name__)

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


# --------------------------------------------------------------------------- #
# low-level signal helpers
# --------------------------------------------------------------------------- #


def _peak_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return DBFS_FLOOR
    peak = float(np.max(np.abs(x)))
    if peak <= 0 or not math.isfinite(peak):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(peak))


def _has_clipped_run(
    x: np.ndarray, *, threshold: float = CLIP_ABS_THRESHOLD, run: int = CLIP_RUN_SAMPLES
) -> bool:
    """True if ``x`` has a run of ``run`` consecutive samples at ≥ full scale."""
    if x.size < run:
        return False
    at_fs = np.abs(x) >= threshold
    if not bool(np.any(at_fs)):
        return False
    # Longest run of True via reset-on-False cumulative counting.
    count = 0
    for flag in at_fs:
        if flag:
            count += 1
            if count >= run:
                return True
        else:
            count = 0
    return False


def _locate(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int,
    max_capture_s: float,
):
    """Matched-filter ``stimulus`` in ``capture``; return the alignment result."""
    return cross_correlation_alignment(
        capture,
        stimulus,
        sample_rate=sample_rate,
        max_capture_s=max_capture_s,
    )


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Delegates to :func:`gating.analytic_envelope` — one implementation."""
    return gating.analytic_envelope(x)


def _subsample_separation(
    capture: np.ndarray,
    arrival_a: int,
    arrival_b: int,
    length: int,
) -> float:
    """Sub-sample separation ``arrival_b − arrival_a`` of two identical stimuli.

    Cross-correlates the two captured windows (same stimulus + same room IR, so
    the peak is sharp) and refines it on the upsampled analytic envelope —
    Gamper's repeat-ratio idea. Returns the refined ``(arrival_b − arrival_a)``.
    """
    from scipy.signal import correlate

    a = np.asarray(capture[arrival_a:arrival_a + length], dtype=np.float64)
    b = np.asarray(capture[arrival_b:arrival_b + length], dtype=np.float64)
    n = min(a.size, b.size)
    if n < 8:
        return float(arrival_b - arrival_a)
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    corr = correlate(b, a, mode="full", method="fft")
    env = _analytic_envelope(corr)
    peak = int(np.argmax(env))
    refined = parabolic_peak(env, peak)
    lag = refined - (n - 1)  # b ≈ a shifted right by lag
    return float((arrival_b - arrival_a) + lag)


def _complex_tf(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int,
    calibration: "CalibrationCurve | None",
):
    """Complex TF of an IR on a fixed grid, with the mic cal folded in (real)."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    H = np.fft.rfft(ir, n=n_fft)
    if calibration is not None:
        correction_db = calibration_mod.apply_calibration_curve(
            freqs, np.zeros_like(freqs), calibration
        )
        H = H * np.power(10.0, correction_db / 20.0)
    return freqs.astype(np.float64), H


def _band_average_db(freqs: np.ndarray, magnitude_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        raise ValueError("overlap band has no frequency bins")
    power = np.power(10.0, magnitude_db[mask] / 10.0)
    return 10.0 * math.log10(max(float(np.mean(power)), 1e-12))


# --------------------------------------------------------------------------- #
# locate + integrity (all phases)
# --------------------------------------------------------------------------- #


def _earliest_strong_peak(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    frac: float = 0.6,
    band_hz: tuple[float | None, float | None] | None = None,
    sample_rate: int | None = None,
) -> int:
    """Index of the EARLIEST normalized-correlation peak within ``frac`` of max.

    A locally energy-normalized matched filter (cosine similarity per lag),
    so a quieter-but-identical first occurrence (MEASURE's woofer repeat) or
    a shape-sharing different-level segment (CHECK's lo/hi pilot pair)
    scores the same as a louder later one; taking the earliest lag within
    ``frac`` of the max picks the true first occurrence.

    ``band_hz`` restricts similarity to the stimulus's OWN declared band —
    without it, room noise the stimulus never occupied suppresses a quiet
    member's score (a quiet pilot once scored below gate despite better
    in-band SNR than a passing round, latching onto the wrong pilot and
    sliding every analysis window one pilot spacing). A caller with no band
    to declare keeps the full-band behavior.
    """
    from scipy.signal import correlate

    cap = np.asarray(capture, dtype=np.float64)
    stim = np.asarray(stimulus, dtype=np.float64)
    cap = cap - cap.mean()
    stim = stim - stim.mean()
    L = stim.size
    if cap.size < L or L == 0:
        return 0
    if (
        band_hz is not None
        and sample_rate
        and band_hz[0] is not None
        and band_hz[1] is not None
    ):
        cap_b = _bandlimit(cap, sample_rate, band_hz[0], band_hz[1])
        stim_b = _bandlimit(stim, sample_rate, band_hz[0], band_hz[1])
        # A band with no surviving bin zeroes both sides; fall back rather
        # than correlate silence against silence.
        if float(np.linalg.norm(stim_b)) > 0.0 and float(np.linalg.norm(cap_b)) > 0.0:
            cap, stim = cap_b, stim_b
    stim_norm = float(np.linalg.norm(stim))
    if stim_norm <= 0.0:
        return 0
    num = correlate(cap, stim, mode="valid", method="fft")
    local_energy = correlate(cap * cap, np.ones(L), mode="valid", method="fft")
    local_norm = np.sqrt(np.maximum(local_energy, 0.0))
    # Floor the denominator so silent (near-zero-energy) windows don't blow the
    # ratio up; a floor at a small fraction of the loudest window is enough.
    floor = 1e-6 * float(local_norm.max()) + 1e-12
    ncc = np.abs(num) / (local_norm * stim_norm + floor)
    peak = float(ncc.max()) if ncc.size else 0.0
    if peak <= 0.0:
        return 0
    return int(np.argmax(ncc >= frac * peak))


def _stimulus_shape(segment: ProgramSegment) -> tuple[float | None, float | None, int]:
    """A stimulus segment's waveform identity — everything
    :func:`segment_stimulus` regenerates it from EXCEPT its level.

    Two segments sharing this triple differ only by amplitude, and
    :func:`_earliest_strong_peak`'s correlation is scale-invariant by
    design, so it cannot distinguish them — the exact ambiguity set
    :func:`_resolve_anchor` arbitrates.
    """
    return (segment.f1_hz, segment.f2_hz, segment.n_samples)


def _resolve_anchor(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    arrival: int,
    first: ProgramSegment,
    stimuli: dict[str, np.ndarray],
) -> tuple[ProgramSegment, int, bool]:
    """Decide WHICH shape-identical stimulus the located ``arrival`` really is,
    and say so when the evidence cannot decide.

    ``_earliest_strong_peak`` answers "where is a stimulus of this shape?"
    but not "which occurrence?", and is level-blind by construction
    (:func:`_stimulus_shape`). Its earliest-lag tie-break is robust for
    equal-level shape-siblings, but the v2 pilot pair is deliberately
    UNEQUAL (VERIFY lo is 10 dB under hi), so the quiet member's local SNR
    can snap the anchor onto the wrong sibling and shift the whole timeline
    by one pilot spacing — beyond the ±30 ms per-segment search window, so
    the rest of the program then reads "not found" on an audible capture.

    So rather than trust one level-blind gate, this enumerates the (few)
    interpretations the schedule permits and asks the capture which one the
    REST of the program agrees with: for each shape-sibling of ``first``,
    reinterpret ``arrival`` as that segment and score the resulting timeline
    by locating an independent WITNESS (the longest stimulus outside the
    ambiguity set) through the same :func:`_locate_in_window` the
    downstream locate uses. Readings are ranked by ``presence``, never
    confidence, since only presence says whether the witness is there.

    This CANNOT manufacture a passing capture: it only changes WHERE the
    analyzer looks; every downstream gate reads the real measured
    correlation. Re-anchoring requires POSITIVE evidence — the winning
    candidate's witness locate must clear ``SWEEP_LOCATE_CONFIDENCE_FLOOR``
    — so a capture with no locatable program declines to move. A program
    with no shape-sibling or no independent witness keeps the unarbitrated
    behavior.

    When the witness cannot tell the interpretations apart, this says so:
    if a near-tie pair (both above the confidence floor, presence within
    :data:`ANCHOR_DISCRIMINATION_RATIO` of each other) separates far less
    than a genuine witness reading does, an argmax between them is a coin
    flip. The committed anchor is left unchanged, but the third return
    value is True, and the CHECK ladder refuses the capture as retriable
    rather than reading a verdict off that flip.
    """
    shape = _stimulus_shape(first)
    candidates = [
        seg for seg in program.segments
        if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) == shape
    ]
    # Longest wins (correlation SNR grows with length); `max` holds its
    # FIRST maximum, so an equal-length tie keeps the earliest segment in
    # schedule order — load-bearing, since a witness confusable with itself
    # under the shift being arbitrated (CHECK's same-duration pilot pairs)
    # would score both hypotheses alike and coin-flip the timeline. Taking
    # the earliest of a tied pair avoids that pair (`_append_leading_pilot_pair`
    # always appends lo-then-hi). This covers only one of the two shift
    # directions; the near-tie guard below covers the other.
    witness = max(
        (seg for seg in program.segments
         if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) != shape),
        key=lambda seg: seg.n_samples,
        default=None,
    )
    if len(candidates) < 2 or witness is None:
        return first, arrival - first.start_sample, False

    witness_stim = stimuli.get(witness.segment_id)
    if witness_stim is None:
        witness_stim = segment_stimulus(witness)
        stimuli[witness.segment_id] = witness_stim
    scored: list[tuple[float, float, ProgramSegment, int]] = []
    for seg in candidates:
        offset = arrival - seg.start_sample
        _located, confidence, presence = _locate_in_window(
            capture, witness_stim, offset + witness.start_sample,
            witness.n_samples, sample_rate=sample_rate,
        )
        scored.append((presence, confidence, seg, offset))
    # Ranked on PRESENCE, not peakedness margin (see docstring); the margin
    # is still read at `corroborated` below. `max` keeps the FIRST maximum
    # so an exact tie holds the structurally first candidate.
    best_index, (best_presence, best_confidence, best_seg, best_offset) = max(
        enumerate(scored), key=lambda item: item[1][0]
    )
    runner_up_presence, runner_up, runner_up_seg, runner_up_offset = max(
        (row for index, row in enumerate(scored) if index != best_index),
        key=lambda item: item[0],
    )
    # Re-anchoring requires POSITIVE evidence that the winning witness locate
    # is a sharp lag, not room noise (a silent driver never played, so
    # nothing in the window is sharp and re-anchoring on noise would shift
    # the timeline for no reason). NOT redundant with the presence ranking
    # (which prefers the later candidate on a garbage capture) and NOT
    # sufficient alone — a sharp lag is not the witness.
    corroborated = best_confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    if not corroborated:
        best_seg, best_offset = first, arrival - first.start_sample
    # Corroboration alone is not discrimination: two candidates both above
    # the floor with presence within ANCHOR_DISCRIMINATION_RATIO of each
    # other means the argmax carries no information (CHECK's witness has a
    # same-shape twin one gap later). The commitment is left unchanged, but
    # flagged un-attributed so a consuming phase refuses it as retriable.
    # Multiplication rather than subtraction so a zero runner-up presence
    # resolves rather than divides by zero.
    ambiguous = (
        corroborated
        and runner_up >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        and best_presence < runner_up_presence * ANCHOR_DISCRIMINATION_RATIO
    )
    corrected = best_seg.segment_id != first.segment_id
    # One line per analyzed capture, naming the losing interpretation too —
    # a reader triaging an ambiguous anchor needs to know which timeline
    # nearly won. `presence=` is the term the choice is made on;
    # `confidence=` is the peakedness margin.
    runner_up_shift_ms = round(
        (runner_up_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
    )
    log_event(
        logger,
        "program_analysis.anchor",
        level=logging.WARNING if (corrected or ambiguous) else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        anchor=best_seg.segment_id,
        witness=witness.segment_id,
        candidates=len(candidates),
        presence=round(best_presence, 6),
        runner_up_presence=round(runner_up_presence, 6),
        confidence=round(best_confidence, 4),
        runner_up=round(runner_up, 4),
        runner_up_anchor=runner_up_seg.segment_id,
        corroborated=corroborated,
        corrected=corrected,
        ambiguous=ambiguous,
        shift_ms=round(
            (best_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
        ),
        runner_up_shift_ms=runner_up_shift_ms,
    )
    return best_seg, best_offset, ambiguous


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray], bool]:
    """Locate the anchor stimulus -> integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ`; the
    coarse arrival is then refined at the full rate inside a tiny window, so
    the returned offset is full-rate-exact. That locate answers WHERE, not
    WHICH occurrence — :func:`_resolve_anchor` arbitrates that and owns the
    returned segment. The fourth return value is its honesty flag: True
    when the evidence could not tell interpretations apart.
    """
    from scipy.signal import resample_poly

    stimuli: dict[str, np.ndarray] = {}
    first = None
    for seg in program.segments:
        if seg.kind in STIMULUS_KINDS:
            first = seg
            break
    if first is None:
        raise ValueError("program has no stimulus segment to locate against")
    stim = segment_stimulus(first)
    stimuli[first.segment_id] = stim
    band_hz = (first.f1_hz, first.f2_hz)

    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(
        capture_lo, stim_lo, band_hz=band_hz, sample_rate=sample_rate // down
    ) * down

    # Full-rate refinement in a +/-4*down window: bounded cost, full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size >= stim.size:
        arrival = lo + _earliest_strong_peak(
            window, stim, band_hz=band_hz, sample_rate=sample_rate
        )
    else:
        arrival = coarse
    anchor, global_offset, ambiguous = _resolve_anchor(
        program, capture, sample_rate, arrival, first, stimuli
    )
    return global_offset, anchor, stimuli, ambiguous


def _locate_in_window(
    capture: np.ndarray,
    stim: np.ndarray,
    scheduled: int,
    n_samples: int,
    *,
    sample_rate: int,
) -> tuple[int, float, float]:
    """Matched-filter ``stim`` at ``scheduled`` +/- :data:`SEGMENT_SEARCH_S`.

    The ONE place the per-segment search geometry lives: both
    :func:`_locate_segments` and :func:`_resolve_anchor` score through it,
    so the chosen anchor is by construction the anchor segments actually
    locate under.

    Returns BOTH scores, since they answer different questions: ``confidence``
    is the peakedness margin (is the winning lag sharp against its own
    neighbourhood — :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR` grades this, NOT
    whether ``stim`` is here at all, since over ~61 ms of lags two room-noise
    correlations already ratio 0.6-0.8); ``presence`` is the normalized
    correlation similarity, which does say. A window too short to hold
    ``stim`` yields ``(scheduled, 0.0, 0.0)``, never a located claim.
    """
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    lo = max(0, scheduled - search)
    hi = min(capture.size, scheduled + n_samples + search)
    window = capture[lo:hi]
    if window.size < stim.size:
        return scheduled, 0.0, 0.0
    res = _locate(
        window, stim, sample_rate=sample_rate,
        max_capture_s=window.size / sample_rate + 1.0,
    )
    return lo + int(res.lag_samples), float(res.confidence), float(res.peak)


def _locate_segments(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    stimuli: dict[str, np.ndarray],
) -> list[SegmentLocation]:
    """Locate every segment at scheduled offset ± window; record integrity."""
    out: list[SegmentLocation] = []
    for seg in program.segments:
        scheduled = global_offset + seg.start_sample
        if seg.kind in STIMULUS_KINDS:
            stim = stimuli.get(seg.segment_id)
            if stim is None:
                stim = segment_stimulus(seg)
                stimuli[seg.segment_id] = stim
            # `presence` is the anchor arbitration's term, not this one's: every
            # gate on `SegmentLocation.confidence` is calibrated on the
            # peakedness margin, so recording the other would move all of them.
            located, confidence, _presence = _locate_in_window(
                capture, stim, scheduled, seg.n_samples, sample_rate=sample_rate,
            )
            seg_samples = capture[located:located + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=located,
                residual_samples=float(located - scheduled),
                confidence=confidence,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
        else:
            seg_samples = capture[max(0, scheduled):scheduled + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=scheduled,
                residual_samples=0.0,
                confidence=1.0,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
    return out


# --------------------------------------------------------------------------- #
# drift (MEASURE)
# --------------------------------------------------------------------------- #

# A MEASURE sweep segment ID's occurrence suffix (build_measure_program's
# _occurrence_suffix): bare = first/primary, "_rep" = second, "_repN" = the
# (N+1)-th. Anchored at the END of the id so a driver token embedded earlier
# (today "w"/"t") never matters here.
_SWEEP_OCCURRENCE_SUFFIX_RE = re.compile(r"_rep(\d*)$")


def _sweep_occurrence_index(segment_id: str) -> int:
    """0-based occurrence index encoded in a MEASURE sweep segment ID's
    suffix (mirrors ``program.build_measure_program``'s ``_occurrence_suffix``):
    bare id ⇒ 0 (first/primary), ``_rep`` ⇒ 1, ``_rep{n}`` ⇒ n (n ≥ 2).
    """
    m = _SWEEP_OCCURRENCE_SUFFIX_RE.search(segment_id)
    if m is None:
        return 0
    digits = m.group(1)
    return 1 if digits == "" else int(digits)


def _sweep_occurrences_by_role(
    locations: Sequence[SegmentLocation],
) -> dict[str, list[SegmentLocation]]:
    """Group MEASURE ``KIND_SWEEP`` locations by driver role, each list
    ordered first->last by ID-encoded occurrence index
    (:func:`_sweep_occurrence_index`), not physical schedule position — the
    N=3 layout interleaves w1,t1,w2,t2,... (design §5.4).
    """
    by_role: dict[str, list[tuple[int, SegmentLocation]]] = {}
    for loc in locations:
        if loc.kind != KIND_SWEEP or not loc.role:
            continue
        by_role.setdefault(loc.role, []).append(
            (_sweep_occurrence_index(loc.segment_id), loc)
        )
    return {
        role: [loc for _idx, loc in sorted(pairs, key=lambda pair: pair[0])]
        for role, pairs in by_role.items()
    }


def _repeat_epsilon(
    capture: np.ndarray,
    program: ExcitationProgram,
    first: SegmentLocation,
    last: SegmentLocation,
) -> tuple[float, float] | None:
    """Sub-sample + integer-only clock-drift epsilon from a role's FIRST vs
    LAST located sweep occurrence (design §3.1/§5.6.3). ``None`` when the
    two share a degenerate scheduled start.
    """
    seg_first = program.segment(first.segment_id)
    seg_last = program.segment(last.segment_id)
    scheduled_sep = seg_last.start_sample - seg_first.start_sample
    if scheduled_sep <= 0:
        return None
    measured_sep = _subsample_separation(
        capture, first.located_start, last.located_start, seg_first.n_samples
    )
    epsilon = measured_sep / scheduled_sep - 1.0
    eps_int = (last.located_start - first.located_start) / scheduled_sep - 1.0
    return epsilon, eps_int


def _locate_discontinuity(
    program: ExcitationProgram,
    capture: np.ndarray,
    stimulus_locs: Sequence[SegmentLocation],
) -> tuple[float | str, str, TimelineStepFit]:
    """Fit a single discrete timeline STEP across the located sweeps.

    Returns ``(step_samples, after_segment_id, fit)``: ``(0.0, "")`` when no
    step is resolved (clean capture); ``(DISCONTINUITY_UNRESOLVED, "")``
    when any ``stimulus_locs`` falls below
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR``, since a step fitted from a barely-found
    sweep is invented from noise. The model lives in
    :mod:`jasper.audio_measurement.timeline_slip`; the sharp input is each
    occurrence placed against its role's first by
    :func:`_subsample_separation` (measured scatter 0.038-0.299 samples)
    rather than the integer ``located_start`` (2.00-3.13 samples on clean
    hardware) — this also cancels the global offset and constant acoustic
    delay structurally.
    """
    ordered = sorted(
        stimulus_locs, key=lambda loc: program.segment(loc.segment_id).start_sample
    )
    # Gate BEFORE the fit, on the confidence the fit is about to trust.
    if any(loc.confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR for loc in ordered):
        return DISCONTINUITY_UNRESOLVED, "", TimelineStepFit()

    # Sub-sample positions, per role, referenced to that role's first
    # occurrence; only WITHIN-role placement needs to be sharp. Keyed by
    # POSITION in `ordered`, not object identity, since locations can compare equal.
    by_role: dict[str, list[int]] = {}
    for index, loc in enumerate(ordered):
        by_role.setdefault(loc.role or "", []).append(index)
    placed: list[float] = [0.0] * len(ordered)
    for members in by_role.values():
        reference = ordered[members[0]]
        ref_n = program.segment(reference.segment_id).n_samples
        placed[members[0]] = float(reference.located_start)
        for index in members[1:]:
            placed[index] = float(reference.located_start) + _subsample_separation(
                capture,
                reference.located_start,
                ordered[index].located_start,
                ref_n,
            )

    fit = fit_timeline_step(
        [float(program.segment(loc.segment_id).start_sample) for loc in ordered],
        placed,
        [loc.role or "" for loc in ordered],
    )
    if not slip_rejects_capture(fit):
        return 0.0, "", fit
    return fit.step_samples, ordered[fit.cut_index - 1].segment_id, fit


def _estimate_drift(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
) -> DriftEstimate:
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    # Only SWEEP-kind stimuli anchor the drift baselines; a leading pilot
    # pair's short/quiet windows locate more coarsely and would manufacture
    # spurious desync (pilots are judged separately, on their own verdict).
    stimulus_locs = [loc for loc in locations if loc.kind == KIND_SWEEP]

    # Primary gate: the WOOFER's first-vs-LAST located occurrence — the one
    # literal anchor kept, since a MEASURE program always contains "sweep_w".
    woofer_role = program.segment("sweep_w").role
    assert woofer_role is not None, "a MEASURE sweep segment always carries a role"
    woofer_occurrences = occurrences_by_role.get(woofer_role, [])
    w1 = woofer_occurrences[0] if woofer_occurrences else None
    w2 = woofer_occurrences[-1] if len(woofer_occurrences) >= 2 else None

    epsilon = 0.0
    if w1 is not None and w2 is not None:
        result = _repeat_epsilon(capture, program, w1, w2)
        if result is not None:
            epsilon = result[0]

    # Per-driver-demeaned schedule residual after applying epsilon. A
    # driver's own acoustic delay is a constant offset (removed by
    # demeaning), so this catches a within-driver desync (a dropped buffer
    # between repeats), not the tweeter-vs-woofer delay. Only activates for
    # a role with >=2 located sweeps. Placed against its group's FIRST by
    # `_subsample_separation` (resolution argument owned by
    # `GLITCH_RESIDUAL_SAMPLES`), never `located_start`.
    groups: dict[Any, list[SegmentLocation]] = {}
    for loc in stimulus_locs:
        groups.setdefault(loc.role, []).append(loc)
    max_residual = 0.0
    for members in groups.values():
        reference = members[0]
        ref_seg = program.segment(reference.segment_id)
        resids = [0.0]
        for loc in members[1:]:
            scheduled_sep = (
                program.segment(loc.segment_id).start_sample - ref_seg.start_sample
            )
            measured_sep = _subsample_separation(
                capture, reference.located_start, loc.located_start, ref_seg.n_samples,
            )
            resids.append(measured_sep - scheduled_sep * (1.0 + epsilon))
        mean = sum(resids) / len(resids)
        for r in resids:
            max_residual = max(max_residual, abs(r - mean))

    # Woofer-repeat LEVEL agreement (design §5.2): first and last sweeps are
    # bit-identical, so a clean capture reproduces the same level. Measured
    # band-relative in-band RMS, never full-band peak — two hardware mics
    # measured identical sweeps 0.64 dB apart by peak but only 0.06-0.24 dB
    # apart by in-band RMS. A larger delta REUSES the
    # drift-baselines-disagree verdict rather than a new code.
    repeat_level_delta_db = 0.0
    repeat_level_disagrees = False
    if w1 is not None and w2 is not None:
        level_seg_w = program.segment("sweep_w")
        if level_seg_w.f1_hz is None or level_seg_w.f2_hz is None:
            raise ValueError("woofer sweep segment has no declared band")
        w1_samples = _pilot_trim_fade(
            capture[w1.located_start:w1.located_start + level_seg_w.n_samples], sample_rate,
        )
        w2_samples = _pilot_trim_fade(
            capture[w2.located_start:w2.located_start + level_seg_w.n_samples], sample_rate,
        )
        level_w1 = _band_rms_dbfs(w1_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        level_w2 = _band_rms_dbfs(w2_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        repeat_level_delta_db = abs(level_w1 - level_w2)
        repeat_level_disagrees = repeat_level_delta_db > REPEAT_LEVEL_TOLERANCE_DB

    # Per-role diagnostics; NEVER gates `glitch_detected` (only the woofer pair does).
    per_role_epsilon_ppm: dict[str, float] = {}
    for role, occurrences in occurrences_by_role.items():
        if len(occurrences) < 2:
            continue
        result = _repeat_epsilon(capture, program, occurrences[0], occurrences[-1])
        if result is not None:
            per_role_epsilon_ppm[role] = result[0] * 1e6

    # Computed on EVERY capture, not just a failing one, for corpus telemetry.
    discontinuity_samples, discontinuity_after, slip_fit = _locate_discontinuity(
        program, capture, stimulus_locs
    )

    # WHICH bound tripped, fixed order — the verdict stays one reason code
    # (§5.2), this is telemetry's disambiguator.
    glitch_inputs = tuple(
        name
        for name, tripped in (
            ("epsilon_out_of_bound", abs(epsilon) * 1e6 > MAX_DRIFT_PPM),
            ("residual_desync", max_residual > GLITCH_RESIDUAL_SAMPLES),
            ("repeat_level_disagree", repeat_level_disagrees),
            (GLITCH_INPUT_TIMELINE_SLIP, slip_rejects_capture(slip_fit)),
        )
        if tripped
    )
    glitch = bool(glitch_inputs)

    if glitch:
        log_event(
            logger,
            "program_analysis.glitch",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            glitch_inputs=",".join(glitch_inputs),
            epsilon_ppm=round(epsilon * 1e6, 2),
            max_residual_samples=round(max_residual, 2),
            repeat_level_delta_db=round(repeat_level_delta_db, 3),
            # `discontinuity_samples` may be `DISCONTINUITY_UNRESOLVED` (a str).
            discontinuity_samples=(
                round(discontinuity_samples, 2)
                if isinstance(discontinuity_samples, (int, float))
                else discontinuity_samples
            ),
            discontinuity_after_segment=discontinuity_after,
        )
    return DriftEstimate(
        epsilon_ppm=epsilon * 1e6,
        max_residual_samples=max_residual,
        glitch_detected=glitch,
        repeat_level_delta_db=repeat_level_delta_db,
        per_role_epsilon_ppm=per_role_epsilon_ppm,
        glitch_inputs=glitch_inputs,
        discontinuity_samples=discontinuity_samples,
        discontinuity_after_segment=discontinuity_after,
    )


# --------------------------------------------------------------------------- #
# capture integrity (VERIFY)
# --------------------------------------------------------------------------- #


def _frame_accounting_checks(ledger: FrameLedger) -> list[IntegrityCheck]:
    """The two frame-accounting checks, most fundamental first.

    ``capture_render_gap`` asks whether the browser's render graph handed the
    recorder every quantum; ``frame_ledger`` asks whether every frame the
    page declared reached this host (see
    :mod:`jasper.audio_measurement.frame_ledger`). A page that reported
    nothing leaves both ``not_evaluated``, never failed —
    ``verification.evaluate_capture_validity`` treats that as usable.
    """
    checks: list[IntegrityCheck] = []
    if not ledger.render_gap_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_RENDER_REPORT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP,
            INTEGRITY_FAIL if ledger.render_gap_frames else INTEGRITY_PASS,
        ))
    if not ledger.balance_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_FRAME_COUNT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER,
            INTEGRITY_PASS if ledger.balanced else INTEGRITY_FAIL,
        ))
    return checks


def _log_frame_ledger(program: ExcitationProgram, ledger: FrameLedger) -> None:
    """One structured line per analyzed capture — the self-report itself.

    Emitted on every phase, including a clean capture (at INFO), so "no
    loss reported" and "no capture analysed" stay distinguishable. A short
    capture is the same line at WARNING.
    """
    lost = ledger.lost_at
    log_event(
        logger,
        "program_analysis.frame_ledger",
        level=logging.WARNING if lost else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        received_frames=ledger.received_frames,
        declared_frames=ledger.declared_frames,
        encoded_frames=ledger.encoded_frames,
        render_gaps=ledger.render_gaps,
        render_gap_frames=ledger.render_gap_frames,
        lost_at=",".join(lost),
    )


def _verify_capture_integrity(
    program: ExcitationProgram,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    frame_ledger: FrameLedger,
) -> CaptureIntegrity:
    """Capture-integrity evidence for a ONE-summed-sweep program.

    ``_estimate_drift`` cannot run here: its three glitch inputs all compare
    a role's repeated sweeps, and VERIFY plays one mono summed sweep. The
    honest record is "drift checks did not run, here is what did".

    What runs, in routing order: (0) frame accounting
    (:func:`_frame_accounting_checks`), ahead of every signal question — a
    capture missing a render quantum can locate its sweep perfectly and
    still be a splice; (1) heard — locate confidence against
    :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR`; (2) schedule — |residual| against
    :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`, only when (1) passed
    (otherwise ``not_evaluated`` with the residual still disclosed); (3)
    clipped run, independent of (1). Pilot segments are excluded from (1)
    and (2) (short/quiet windows locate coarsely) and included in (3).

    What (2) cannot see: a splice INSIDE the summed sweep (the residual is
    measured at the located START; needs more sweeps than VERIFY has — (0)
    only closes the browser-visible half of this class); a splice BEFORE
    the first stimulus (absorbed by the global offset, correctly, since a
    uniformly shifted capture is not corrupt); and anything on a
    pilot-less VERIFY program, where the summed sweep IS the anchor and its
    residual is structurally ~0 (every session-composed VERIFY carries a
    leading pilot pair instead).
    """
    sweeps = [loc for loc in locations if loc.kind == KIND_SUMMED_SWEEP]
    stimuli = [loc for loc in locations if loc.kind in STIMULUS_KINDS]
    clipped_segments = tuple(loc.segment_id for loc in stimuli if loc.clipped)

    confidence_min: float | None = None
    residual_ms_worst: float | None = None
    if sweeps:
        confidence_min = min(float(loc.confidence) for loc in sweeps)
        worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
        residual_ms_worst = float(worst.residual_samples) / sample_rate * 1000.0

    checks: list[IntegrityCheck] = _frame_accounting_checks(frame_ledger)
    if confidence_min is None or residual_ms_worst is None:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
    elif confidence_min < SWEEP_LOCATE_CONFIDENCE_FLOOR:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_FAIL))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_SWEEP_NOT_HEARD,
        ))
    else:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_PASS))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE,
            INTEGRITY_FAIL
            if abs(residual_ms_worst) > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
            else INTEGRITY_PASS,
        ))
    if not stimuli:
        # No stimulus segment to inspect is not "nothing was clipped" — it is
        # the same "nobody looked" a bare False would have been.
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_STIMULUS,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN,
            INTEGRITY_FAIL if clipped_segments else INTEGRITY_PASS,
        ))
    for name in (
        INTEGRITY_CHECK_REPEAT_EPSILON,
        INTEGRITY_CHECK_REPEAT_LEVEL,
        INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    ):
        checks.append(IntegrityCheck(
            name, INTEGRITY_NOT_EVALUATED, _INTEGRITY_NO_REPEAT_PAIR,
        ))
    checks.append(IntegrityCheck(
        INTEGRITY_CHECK_DISCONTINUITY_STEP, INTEGRITY_NOT_EVALUATED,
        _INTEGRITY_STEP_NEEDS_MORE_SWEEPS,
    ))

    integrity = CaptureIntegrity(
        checks=tuple(checks),
        locate_confidence_min=confidence_min,
        schedule_residual_ms_worst=residual_ms_worst,
        clipped_segments=clipped_segments,
    )
    if integrity.glitched:
        # The VERIFY twin of ``program_analysis.glitch``, at the same level
        # and for the same reason: the capture this fired on is about to be
        # refused, and the journal should say which measurement said so.
        log_event(
            logger,
            "program_analysis.capture_integrity",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            failed=",".join(integrity.failed),
            not_evaluated=",".join(integrity.not_evaluated),
            locate_confidence_min=(
                round(confidence_min, 4) if confidence_min is not None else None
            ),
            schedule_residual_ms_worst=(
                round(residual_ms_worst, 3) if residual_ms_worst is not None else None
            ),
            clipped_segments=",".join(clipped_segments),
        )
    return integrity


# --------------------------------------------------------------------------- #
# per-driver response + alignment + candidate (MEASURE)
# --------------------------------------------------------------------------- #


def _deconvolve_window(
    capture: np.ndarray,
    segment: ProgramSegment,
    anchor: int,
    sample_rate: int,
    *,
    epsilon: float = 0.0,
    pre_guard_s: float = DECONV_PRE_GUARD_S,
    tail_s: float = 0.5,
) -> tuple[np.ndarray, int]:
    """Deconvolve one sweep → ``(full_ir, pre_guard_samples)``.

    The window starts ``pre_guard_samples`` before ``anchor`` (the scheduled
    capture position ``global_offset + start``), so it fully contains the sweep
    even though the global offset folds in the first driver's small acoustic
    delay. With a shared anchor + pre-guard across drivers, each deconvolved IR's
    direct peak lands at ``pre_guard_samples`` ± the relative delay.

    ``epsilon`` divides the measured clock drift out (design §3.1): the captured
    sweep is stretched by ``(1+ε)``, so the reference is resampled to match
    before inversion — keeping the deconvolution sharp (and the delay estimate
    accurate) under drift instead of smearing the IR.
    """
    stim = segment_stimulus(segment)
    if epsilon != 0.0:
        from scipy.signal import resample

        stretched_len = int(round(stim.size * (1.0 + epsilon)))
        if stretched_len > 0:
            stim = resample(np.asarray(stim, dtype=np.float64), stretched_len)
    pre = int(round(pre_guard_s * sample_rate))
    tail = int(round(tail_s * sample_rate))
    window_start = anchor - pre
    lo = max(0, window_start)
    pre_effective = anchor - lo  # shrinks if the window clamps at the capture head
    hi = min(capture.size, anchor + segment.n_samples + tail)
    window = np.asarray(capture[lo:hi], dtype=np.float64)
    if window.size < stim.size:
        raise ValueError(f"deconvolution window for {segment.segment_id!r} too short")
    full_ir = deconv.regularized_deconvolution_full(
        window, np.asarray(stim, dtype=np.float64), sample_rate
    )
    return full_ir, pre_effective


def _gate_floor_hz(fragment: Mapping[str, Any]) -> float | None:
    """Validity floor from a gate fragment, or ``None`` when ungateable.

    Shared by every caller that windows an IR through
    :func:`gating.gate_impulse_response`: ``floor_source is None`` means the
    IR was never gated (silent/NaN capture, no room to search), so the
    fragment's ``f_valid_floor_hz`` key is not a real floor even though it's
    present — mirrors :mod:`gating`'s own ``applied`` rule.
    """
    if fragment.get("floor_source") is None:
        return None
    floor = fragment.get("f_valid_floor_hz")
    return float(floor) if isinstance(floor, (int, float)) else None


def _radiated_band_hz(segment: Any) -> tuple[float, float] | None:
    """The band a sweep segment actually drove, for the gate's disclosure.

    A call-site seam, not policy: the band POLICY (intersecting this with
    the caller's gate floor) belongs to
    :func:`jasper.audio_measurement.gate_disclosure.evaluation_band_hz`.
    This only reads what the excitation program already declares, and
    returns ``None`` for a segment that declares no sweep bounds so the
    delta is omitted rather than computed over a made-up band.
    """
    lo, hi = getattr(segment, "f1_hz", None), getattr(segment, "f2_hz", None)
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    lo, hi = float(lo), float(hi)
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
        return None
    return lo, hi


def _raw_sweep_segment(
    capture: np.ndarray, segment: ProgramSegment, anchor: int,
) -> np.ndarray:
    """The raw captured samples of one sweep segment, at the SAME schedule
    anchor :func:`_deconvolve_window` uses.

    Deliberately the scheduled window rather than the located one: the SNR
    verdict describes the response this anchor produced, so a level read
    somewhere else would be describing a different capture. Clamped to the
    capture, so a short recording yields a short (or empty) segment instead
    of raising — the SNR verdict is diagnostic, and the locator is what
    fails a truncated capture.

    The ``max(lo, ...)`` in the stop is load-bearing: without it, an ``anchor``
    far enough before the capture that ``anchor + n_samples`` lands in
    ``(-capture.size, 0)`` gives a NEGATIVE stop, which numpy reads as an
    offset from the END — so the function would return a non-empty slice of
    some other part of the recording and the SNR verdict would state a
    confident number about audio this sweep never played. No production caller
    reaches that (every anchor is ``global_offset + segment.start_sample``,
    both non-negative), so this guards the contract rather than a live path.
    """
    lo = max(0, anchor)
    hi = min(capture.size, max(lo, anchor + segment.n_samples))
    return np.asarray(capture[lo:hi], dtype=np.float64)


def _driver_snr_block(
    *,
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    freqs: np.ndarray,
    mag_db: np.ndarray,
    capture_segment: np.ndarray | None,
    sample_rate: int,
    radiated_band_hz: tuple[float, float] | None,
) -> dict[str, Any] | None:
    """The per-driver magnitude SNR verdict, read in ONE domain.

    Both sides of an SNR subtraction must be the same quantity: a
    ``"deconvolved"`` noise report pairs with deconvolved transfer-function
    levels; a ``"raw"`` report (every report a v2 CHECK hands forward)
    pairs with the RAW captured sweep's band levels. This matches
    :func:`_solve_role_gain`'s own RAW-domain aim, so a raw-with-raw verdict
    reads back the exact quantity the solve targeted. Mixing domains is
    unsafe: on synthetic fixtures a MEASURE played 20 dB quieter reported
    the SAME worst-band SNR in the cross-domain reading (deconvolution
    cancels drive level, the room's dBFS floor does not) while the
    same-domain reading fell the full 20 dB.

    ``window="rectangular"`` because a sweep is non-stationary (a Hann
    window re-weights by WHEN frequencies occur); the padded-capture
    duty-cycle offset that makes rectangular unsafe elsewhere is zero here
    since :func:`_raw_sweep_segment` hands exactly ``segment.n_samples``.

    ``radiated_band_hz`` scopes the verdict to the band this branch's
    stimulus actually drove (:func:`~..comparison_bands.branch_snr_band_hz`).
    Fails closed: a raw report with no captured segment, or a branch with
    no corner/radiated band, produces no verdict at all. A present-but-degenerate
    segment instead yields ``verdict: "unknown"`` with an empty band list —
    absent means "no evidence offered", unknown means "evidence was
    unusable".
    """
    if ambient_report is None:
        return None
    relevant_hz = branch_snr_band_hz(fc_hz, radiated_band_hz)
    if relevant_hz is None:
        return None
    noise_domain, noise_bands = snr_policy.unwrap_noise_report(ambient_report)
    if noise_domain == "deconvolved":
        capture_bands = snr_policy.magnitude_band_levels(freqs, mag_db)
        band_method = "deconvolved_band_difference"
    elif capture_segment is not None:
        capture_bands = snr_policy.band_levels_dbfs(
            capture_segment,
            sample_rate,
            snr_policy.CROSSOVER_SNR_BANDS_HZ,
            window="rectangular",
        )
        band_method = "fft_band_power_difference"
    else:
        return None
    block = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=relevant_hz,
        model=DRIVER,
        band_method=band_method,
    )
    # TWO decision classes off ONE set of measurements: the magnitude
    # verdict above grades ok/reduced/insufficient around 25/20 dB; a
    # POLARITY/DELAY decision is held to the stricter 35 dB
    # `DRIVER.alignment_snr_ok_db` (no reduced rung) — reusing the magnitude
    # verdict would ship an unrefused polarity read off an unusable capture.
    block[DRIVER_SNR_ALIGNMENT_KEY] = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=relevant_hz,
        model=DRIVER,
        band_method=band_method,
    )
    return block


def _driver_response(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
    radiated_band_hz: tuple[float, float] | None = None,
    capture_segment: np.ndarray | None = None,
) -> DriverResponse:
    """One role's gated, calibrated response plus the gate's own disclosure.

    ``radiated_band_hz`` is the band this capture's excitation actually drove —
    the caller's segment sweep bounds. It is the ONLY input the pre/post-gate
    delta needs beyond the IR, and it is threaded from here rather than guessed
    downstream (see :mod:`jasper.audio_measurement.gate_disclosure`). Absent,
    the delta is simply not reported — never defaulted. It has a SECOND reader:
    :func:`_driver_snr_block` scopes the capture-SNR verdict to the same band,
    so a row this stimulus deliberately left empty cannot veto it
    (:func:`branch_snr_band_hz`). One declared fact, two consumers — neither
    re-derives the sweep's edges.

    ``capture_segment`` is the RAW captured samples of this role's sweep —
    the signal side of the SNR verdict whenever the noise report is a raw
    one. See :func:`_driver_snr_block` for why the verdict cannot be built
    from ``full_ir`` in that case.
    """
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    applied = fragment["floor_source"] is not None
    delta = gate_disclosure.pre_post_gate_delta(
        ir, gated_ir, sample_rate,
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=radiated_band_hz,
    )
    gating_block = {
        "applied": applied,
        "exempt_reason": None,
        **fragment,
        "pre_post_gate_delta": delta,
    }
    validity_floor_hz = _gate_floor_hz(fragment)

    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    mag_db = 20.0 * np.log10(np.maximum(np.abs(H), 1e-12))

    snr_block = _driver_snr_block(
        ambient_report=ambient_report,
        fc_hz=fc_hz,
        freqs=freqs,
        mag_db=mag_db,
        capture_segment=capture_segment,
        sample_rate=sample_rate,
        radiated_band_hz=radiated_band_hz,
    )
    return DriverResponse(
        role=role,
        freqs_hz=freqs,
        magnitude_db=mag_db,
        complex_tf=H,
        gating=gating_block,
        snr=snr_block,
        validity_floor_hz=validity_floor_hz,
    )


def _aligned_branch_tf(
    full_ir: np.ndarray,
    sample_rate: int,
    n_fft: int,
    *,
    calibration: "CalibrationCurve | None",
):
    """Delay-referenced, gating-consistent complex TF for the sum prediction.

    :func:`deconv.direct_arrival_window` places each branch's direct peak at
    the same fixed offset inside the window (bulk delay removed) without a
    circular roll, which would inject a spurious echo. The windowed IR then
    runs through the SAME adaptive reflection gate :func:`_driver_response`
    applies — a fixed window alone bakes a room reflection into the
    predicted sum that VERIFY's measured sum has already gated out (traced
    once to a 15 cm desk-bounce producing a spurious ~1125 Hz null).
    """
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    return freqs, H, fragment


def _estimate_alignment(
    capture: np.ndarray,
    program: ExcitationProgram,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    fc_hz: float,
    geometry: MeasurementGeometry,
    priors: MeasurementPriors,
    *,
    woofer_full_ir: np.ndarray,
    tweeter_full_ir: np.ndarray,
    pre_samples: int,
) -> AlignmentEstimate:
    seg_w = program.segment("sweep_w")
    seg_t = program.segment("sweep_t")
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    )

    max_lag = priors.align_search_ms * 1e-3 * sample_rate
    # Both IRs share the pre-guard + global offset time base, so each direct
    # peak sits at pre_samples +/- the relative delay. Slice the same
    # [pre-H, pre+H] region from both, band-limit to the overlap, GCC-PHAT.
    half = int(round(0.010 * sample_rate)) + int(math.ceil(max_lag)) + 1
    a = max(0, pre_samples - half)
    b_w = min(woofer_full_ir.size, pre_samples + half)
    b_t = min(tweeter_full_ir.size, pre_samples + half)
    b = min(b_w, b_t)
    ir_w = _bandlimit(np.asarray(woofer_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    ir_t = _bandlimit(np.asarray(tweeter_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    length = min(ir_w.size, ir_t.size)
    ir_w, ir_t = ir_w[:length], ir_t[:length]

    lag_samples, polarity_sign, confidence, at_edge = gcc_phat(
        ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, max_lag_samples=max_lag,
    )
    # epsilon-correct: the tweeter's schedule offset is stretched by epsilon.
    delta_start = seg_t.start_sample - seg_w.start_sample
    tau_samples = lag_samples - epsilon * delta_start
    # delay_us = (D_woofer - D_tweeter) = -tau (tau = D_tweeter - D_woofer).
    raw_delay_us = -tau_samples / sample_rate * 1e6
    parallax_us = geometry.parallax_us()
    delay_us = raw_delay_us - parallax_us

    polarity = polarity_label(polarity_sign)

    status = ALIGNMENT_OK
    if at_edge:
        # A peak clamped at the search bound likely exceeds the geometry
        # prior; fail explicitly rather than return a wrong value.
        status = ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
        confidence = 0.0
        log_event(
            logger,
            "program_analysis.alignment_edge",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            lag_samples=round(lag_samples, 3),
            search_window_ms=priors.align_search_ms,
        )

    # Fine stage (methodology §10). The aligner OWNS the physical peak-gap
    # anchor (raw full-IR argmax gap, drift+parallax-corrected); the argmax
    # is never recomputed downstream so the snap center and the reported
    # anchor cannot desync. The snap moves the anchor to the nearest local
    # maximum of the same correlation within +/-(period/6) at Fc; ``None``
    # leaves ``_build_candidate`` on the bare anchor. Snaps applied delay
    # only — GCC polarity/confidence is untouched.
    snapped_delay_us: float | None = None
    anchor_delay_us: float | None = None
    if status == ALIGNMENT_OK:
        anchor_lag_samples = float(
            int(np.argmax(np.abs(tweeter_full_ir)))
            - int(np.argmax(np.abs(woofer_full_ir)))
        )
        # Peak gap - inter-sweep drift, plus parallax, negated into the signed frame.
        inter_sweep_drift_us = epsilon * delta_start / sample_rate * 1e6
        drift_corrected_peak_gap_us = (
            anchor_lag_samples / sample_rate * 1e6 - inter_sweep_drift_us
        )
        anchor_delay_us = -(drift_corrected_peak_gap_us + parallax_us)
        if fc_hz > 0.0:
            radius_samples = sample_rate / fc_hz * GCC_SNAP_RADIUS_PERIODS
            # Recomputes the correlation rather than threading the seed's
            # array: one extra small FFT per MEASURE, no big-array coupling.
            snapped_lag = _gcc_local_peak_snap(
                ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
                upsample=GCC_UPSAMPLE, anchor_lag_samples=anchor_lag_samples,
                radius_samples=radius_samples,
            )
            if snapped_lag is not None:
                snapped_tau = snapped_lag - epsilon * delta_start
                snapped_delay_us = -snapped_tau / sample_rate * 1e6 - parallax_us

    # The flat-sum cross-check belongs to `_select_alignment_pair`; this
    # estimate is the correlation SEED, and `polarity_agrees_with_sum`
    # stays None until the selection answers it.
    return AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=raw_delay_us,
        parallax_us=parallax_us,
        polarity=polarity,
        polarity_sign=polarity_sign,
        confidence=confidence,
        status=status,
        anchor_delay_us=anchor_delay_us,
        snapped_delay_us=snapped_delay_us,
    )


def predicted_branch_sum(
    W: np.ndarray,
    T: np.ndarray,
    trim_w_db: float,
    trim_t_db: float,
    sign: int,
    *,
    freqs_hz: np.ndarray | None = None,
    residual_delay_us: float = 0.0,
) -> np.ndarray:
    """Return the complex branch sum in the argmax-referenced frame.

    ``_aligned_branch_tf`` independently references both direct peaks, so a
    physical applied delay must enter here only as the *residual* relative
    to that frame — passing the full applied delay would count the
    measured peak gap twice. Callers compute the residual via the ONE
    owner, :func:`summed_model_residual_delay_us`. Public because the v2
    session reuses this for its LINEARIZED-branch VERIFY prediction.
    """
    g_w = 10.0 ** (trim_w_db / 20.0)
    g_t = 10.0 ** (trim_t_db / 20.0)
    tweeter = T * g_t
    if freqs_hz is not None and residual_delay_us != 0.0:
        tweeter = tweeter * np.exp(
            -1j * 2.0 * np.pi * np.asarray(freqs_hz) * residual_delay_us * 1e-6
        )
    return W * g_w + sign * tweeter


def summed_model_residual_delay_us(
    anchor_delay_us: float | None, applied_delay_us: float
) -> float:
    """The ONE derivation of :func:`predicted_branch_sum`'s ``residual_delay_us``.

    ``(D_t - D_w) + applied_signed_delay``, expressed via the aligner's two
    numbers: ``-anchor_delay_us`` IS ``(D_t - D_w)``, so the residual is
    ``applied - anchor``, exactly ``0.0`` at the bare anchor.
    :func:`_aligned_branch_tf` already references each branch to its own
    direct peak, so phasing by the FULL applied delay would count the
    measured peak gap twice and inject a deep comb into the predicted sum.

    ``anchor_delay_us`` is ``None`` when the aligner refused the estimate
    (no trustworthy argmax-frame reference, and no delay is applied
    either), or when a capture's committed delay came from the low-SNR arm
    of :func:`_select_alignment_pair` (a disagreement between an untrusted
    anchor and a trusted applied delay, not a fact about the speaker). Both
    cases return ``0.0``: the model keeps the independently-aligned frame
    rather than fabricate a comb the emitted graph need not have — this
    feeds VERIFY's tracking reference and prediction gate, so a speaker
    that really combs still fails honestly on the absolute claim.
    """
    if anchor_delay_us is None:
        return 0.0
    return float(applied_delay_us) - float(anchor_delay_us)


def half_period_us(fc_hz: float) -> float:
    """Half of one period at ``fc_hz``, in microseconds. ONE spelling.

    The radius of the comb lobe a crossover corner owns, and therefore the
    delay-ambiguity budget: two delays further apart than this put the
    summation on adjacent lobes. Shared by ``_select_alignment_pair``'s
    ``left_anchor_lobe`` tripwire and
    :func:`jasper.active_speaker.crossover_v2.alignment_prescription.read_alignment_prescription`'s
    bound. ``fc_hz`` must be positive and finite; callers guard first.
    """
    return 0.5e6 / float(fc_hz)


def _ripple_db(freqs: np.ndarray, magnitude: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return float("inf")
    band = magnitude[mask]
    band_db = 20.0 * np.log10(np.maximum(np.abs(band), 1e-12))
    return float(np.max(band_db) - np.min(band_db))


def _finite_or_none(value: float, ndigits: int) -> float | None:
    """``round(value, ndigits)``, or ``None`` when it is not a finite number.

    For journal fields that quote a measured quantity which CAN be non-finite:
    ``None`` reads as "no number", where a bare NaN reads as a number and
    survives into whatever parses the line.
    """
    return round(float(value), ndigits) if math.isfinite(value) else None


def polarity_label(polarity_sign: int) -> str:
    """``+1 -> "normal"``, ``-1 -> "inverted"``. The ONE spelling of the map."""
    return "normal" if polarity_sign >= 0 else "inverted"


def polarity_sign_of(polarity: str) -> int:
    """Inverse of :func:`polarity_label`; anything but ``"inverted"`` is ``+1``."""
    return -1 if polarity == "inverted" else 1


@dataclass(frozen=True)
class AlignmentPairSelection:
    """What :func:`_select_alignment_pair` committed, and the evidence for it.

    ``polarity_sign``/``delay_us`` are the committed pair; ``ripple_db`` is
    the summed blend ripple there. ``seed_*`` is the pair correlation alone
    would have shipped, scored on the SAME objective; ``objective`` names
    which commitment this is (:data:`ALIGNMENT_COMMITMENTS`).
    ``grid_points``/``grid_step_us`` describe the delay grid searched per
    polarity (``1``/``0.0`` when nothing was searched).

    ``left_anchor_lobe`` is True when the committed delay sits more than
    half a period at Fc from the anchor — legitimate when the objective is
    that sure, but also the shape a fooled objective would take, so it
    raises the selection log to WARNING.
    """

    polarity_sign: int
    delay_us: float
    ripple_db: float
    seed_polarity_sign: int
    seed_delay_us: float
    seed_ripple_db: float
    objective: str
    grid_points: int
    grid_step_us: float
    left_anchor_lobe: bool = False
    #: Was the POLARITY axis pinned by the request rather than searched?
    #: The objective string can't carry this (a pinned round still commits
    #: ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION); read via
    #: :attr:`polarity_agrees_with_sum` instead.
    polarity_pinned: bool = False

    @property
    def polarity_agrees_with_sum(self) -> bool | None:
        """Did correlation's polarity answer survive the flat-sum objective?

        ``None`` on any commitment the flat-sum objective did not make on
        the polarity axis (low-SNR path, or a pinned round) — recording
        ``False`` there would report a comparison that never happened.
        """
        if self.polarity_pinned:
            return None
        if self.objective not in _FLAT_SUM_POLARITY_OBJECTIVES:
            return None
        return self.polarity_sign == self.seed_polarity_sign

    @property
    def flatness_improvement_db(self) -> float:
        """``seed_ripple - committed_ripple``: what the objective bought."""
        return self.seed_ripple_db - self.ripple_db


def _select_alignment_pair(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    *,
    fc_hz: float,
    lo_hz: float,
    hi_hz: float,
    trim_w_db: float,
    trim_t_db: float,
    anchor_delay_us: float | None,
    seed_delay_us: float,
    seed_polarity_sign: int,
    delay_bounds_us: tuple[float, float] | None = None,
    branch_snr_insufficient: bool = False,
    applied_alignment: AppliedAlignment | None = None,
    explicit_delay_us: float | None = None,
    explicit_polarity_sign: int | None = None,
) -> AlignmentPairSelection | None:
    """Commit the (polarity, delay) pair whose predicted blend sums flattest.

    ONE objective for both halves of one decision: the ripple of ``W + s*T``
    over ``[lo_hz, hi_hz]``, scored across ``s in {+1, -1}`` and a delay
    grid of :data:`ALIGNMENT_FLATNESS_STEP_US` steps spanning
    +/-:data:`ALIGNMENT_FLATNESS_SPAN_PERIODS` period(s) at ``fc_hz`` around
    ``anchor_delay_us`` — polarity and delay trade against each other, so
    scoring them separately would compare one against a guess about the
    other. Correlation stays in the loop as the SEED and tie-break: within
    :data:`ALIGNMENT_FLAT_MINIMUM_EPSILON_DB` of the global minimum, the
    search keeps whichever is closest to the seed.

    ``trim_w_db``/``trim_t_db`` are the LEVEL-MATCH trims (band-average),
    not the ripple-polished tweeter trim — the polish needs a polarity, so
    scoring at the polished trim would be circular.

    ``delay_bounds_us`` is the preset's declared |delay| range; grid points
    outside it are dropped (the seed pair is exempt).

    ``branch_snr_insufficient`` is the refusal
    (:data:`ALIGNMENT_SNR_REFUSAL_VERDICT`): the pair is not searched but
    COMMITTED to the declared design (relative polarity ``+1``) at a delay
    this capture did not supply — never the anchor or GCC seed, which are
    this capture's own answer and exactly what a low-SNR capture gets
    wrong (across nine jts3 positions the anchor read +59.6 us on-axis
    against six clustered near -211 us and two wild). The delay comes from
    ``applied_alignment`` (what the speaker already plays) or ``0.0``; three
    arms/objectives distinguish held/declared/unreadable-apply.

    ``left_anchor_lobe`` fires when the committed delay and this capture's
    anchor disagree by more than half a period at Fc, raising the selection
    log to WARNING — legitimate when the objective is that sure, but also
    the shape a fooled objective would take.

    ``explicit_delay_us`` is a host-validated PRESCRIPTION (delay from a
    named measured basis, bounded to +/-half a period at Fc); it fixes the
    delay axis to exactly that point and outranks the low-SNR ladder.
    Deliberately NOT an anchor substitute — re-centring the search on it
    would let the objective wander off the prescribed value.
    ``explicit_polarity_sign`` pins the other axis the same way (on both
    prescription arms), recording :attr:`~AlignmentPairSelection.polarity_pinned`
    so ``polarity_agrees_with_sum`` reports ``None`` rather than a search
    result that never ran.

    Returns ``None`` when the objective cannot be evaluated at all (no
    frequency bin in range, or no finite-score candidate), leaving the
    caller on the seed with a WARNING.
    """
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(band):
        return None
    freqs_band = freqs[band]
    W_band = W[band]
    T_band = T[band]

    def _ripple_at(polarity_sign: int, delay_us: float) -> float:
        summed = predicted_branch_sum(
            W_band, T_band, trim_w_db, trim_t_db, polarity_sign,
            freqs_hz=freqs_band,
            residual_delay_us=summed_model_residual_delay_us(
                anchor_delay_us, delay_us,
            ),
        )
        return _ripple_db(freqs_band, summed, lo_hz, hi_hz)

    def _left_anchor_lobe(delay_us: float) -> bool:
        """Did the commitment leave the comb lobe the anchor owns?

        Both Fc guards needed: ``not (fc_hz > 0.0)`` rejects zero/negative/NaN;
        ``isfinite`` also rejects ``+inf``, which would make
        :func:`half_period_us` return ``0.0`` and fire on every commitment.
        """
        if anchor_delay_us is None or not (fc_hz > 0.0) or not math.isfinite(fc_hz):
            return False
        return abs(delay_us - anchor_delay_us) > half_period_us(fc_hz)

    seed_ripple_db = _ripple_at(seed_polarity_sign, seed_delay_us)

    if explicit_delay_us is not None and branch_snr_insufficient:
        # The prescription stands (it did not come from this capture); an
        # unpinned polarity commits declared relative polarity +1 instead.
        prescribed_us = float(explicit_delay_us)
        prescribed_sign = (
            1 if explicit_polarity_sign is None else int(explicit_polarity_sign)
        )
        return AlignmentPairSelection(
            polarity_sign=prescribed_sign,
            delay_us=prescribed_us,
            ripple_db=_ripple_at(prescribed_sign, prescribed_us),
            seed_polarity_sign=seed_polarity_sign,
            seed_delay_us=seed_delay_us,
            seed_ripple_db=seed_ripple_db,
            objective=ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
            grid_points=1,
            grid_step_us=0.0,
            left_anchor_lobe=_left_anchor_lobe(prescribed_us),
            polarity_pinned=explicit_polarity_sign is not None,
        )

    if branch_snr_insufficient:
        # Neither `anchor_delay_us` nor `seed_delay_us` may be read here:
        # both are this capture's own answer, and this capture was refused.
        held_delay_us = (
            None if applied_alignment is None else applied_alignment.delay_us
        )
        committed_delay_us = 0.0 if held_delay_us is None else float(held_delay_us)
        if held_delay_us is not None:
            objective = ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
        elif applied_alignment is None:
            objective = ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
        else:
            objective = ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY
        return AlignmentPairSelection(
            polarity_sign=1,
            delay_us=committed_delay_us,
            ripple_db=_ripple_at(1, committed_delay_us),
            seed_polarity_sign=seed_polarity_sign,
            seed_delay_us=seed_delay_us,
            seed_ripple_db=seed_ripple_db,
            objective=objective,
            grid_points=1,
            grid_step_us=0.0,
            left_anchor_lobe=_left_anchor_lobe(committed_delay_us),
        )

    # The delay is only scorable against an anchor: with none, the residual
    # is 0.0 for every candidate, so search the polarity alone.
    grid_step_us = 0.0
    delays = [seed_delay_us]
    # A prescription fixes the delay axis to exactly one point. The seed is
    # NOT appended here (unlike elsewhere): that would let the search
    # silently return the seed's delay instead of the prescribed one.
    if explicit_delay_us is not None:
        delays = [float(explicit_delay_us)]
    elif anchor_delay_us is not None and fc_hz > 0.0:
        span_us = ALIGNMENT_FLATNESS_SPAN_PERIODS * 1e6 / fc_hz
        n_steps = int(round(span_us / ALIGNMENT_FLATNESS_STEP_US))
        n_steps = max(1, min(n_steps, ALIGNMENT_FLATNESS_MAX_STEPS))
        step_us = span_us / n_steps
        grid = [anchor_delay_us + i * step_us for i in range(-n_steps, n_steps + 1)]
        if delay_bounds_us is not None:
            lo_us, hi_us = (abs(float(b)) for b in delay_bounds_us)
            lo_us, hi_us = min(lo_us, hi_us), max(lo_us, hi_us)
            grid = [d for d in grid if lo_us <= abs(d) <= hi_us]
        # An empty grid (bound admits no point) reports like no-anchor: (1, 0.0).
        if grid:
            grid_step_us = step_us
        delays = [*grid, seed_delay_us]

    # The polarity axis, pinned the same way: the seed sign is not added
    # back, for the same reason the seed delay is not.
    signs = (
        (1, -1) if explicit_polarity_sign is None
        else (int(explicit_polarity_sign),)
    )
    pairs = [(sign, delay) for sign in signs for delay in delays]
    # Non-finite scores are not candidates (only fires on a branch TF that
    # already carries NaN/inf).
    scored = [
        (sign, delay, ripple)
        for (sign, delay), ripple in (
            (pair, _ripple_at(*pair)) for pair in pairs
        )
        if math.isfinite(ripple)
    ]
    if not scored:
        log_event(
            logger, "program_analysis.alignment_not_scorable",
            level=logging.WARNING,
            reason="no_finite_ripple",
            fc_hz=round(float(fc_hz), 3),
            band_hz=(round(float(lo_hz), 1), round(float(hi_hz), 1)),
            candidates=len(pairs),
        )
        return None
    best_ripple = min(ripple for _s, _d, ripple in scored)
    # Flat-minimum regularization: within epsilon of the global minimum,
    # keep the seed's polarity first, then the delay closest to the seed.
    committed_sign, committed_delay_us, committed_ripple_db = min(
        (
            item for item in scored
            if item[2] <= best_ripple + ALIGNMENT_FLAT_MINIMUM_EPSILON_DB
        ),
        key=lambda item: (
            0 if item[0] == seed_polarity_sign else 1,
            abs(item[1] - seed_delay_us),
        ),
    )
    return AlignmentPairSelection(
        polarity_sign=committed_sign,
        delay_us=committed_delay_us,
        ripple_db=committed_ripple_db,
        seed_polarity_sign=seed_polarity_sign,
        seed_delay_us=seed_delay_us,
        seed_ripple_db=seed_ripple_db,
        objective=(
            ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
            if explicit_delay_us is not None
            else ALIGNMENT_COMMITTED_FLAT_SUM
        ),
        grid_points=len(delays),
        grid_step_us=grid_step_us,
        left_anchor_lobe=_left_anchor_lobe(committed_delay_us),
        polarity_pinned=explicit_polarity_sign is not None,
    )


def branch_level_bands_hz(
    fc_hz: float,
    *,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """``((woofer_lo, woofer_hi), (tweeter_lo, tweeter_hi))`` — the two
    log-symmetric half-bands the level match reads, one per branch.

    Each ``*_span_hz`` is that branch's OWN validity span (excited band,
    narrowed by any reflection-gate floor), never the shared overlap — a
    branch is read only where it played. Returns ``[Fc/rho, Fc]`` (woofer)
    and ``[Fc, Fc*rho]`` (tweeter) with the largest ``rho <=
    OVERLAP_OCTAVE_RATIO`` fitting inside BOTH spans. Both inner edges are
    load-bearing (halves meet AT Fc): a span that does not reach Fc raises,
    through the same ``internal_error`` seam as an unanalysable capture,
    never a guessed trim.

    SSOT for the band pair: :func:`solve_branch_trims` computes the levels
    and ``_build_candidate`` discloses the bands from this one derivation.
    """
    w_lo_bound, w_hi_bound = (
        woofer_span_hz
        if woofer_span_hz is not None
        else (fc_hz / OVERLAP_OCTAVE_RATIO, fc_hz)
    )
    t_lo_bound, t_hi_bound = (
        tweeter_span_hz
        if tweeter_span_hz is not None
        else (fc_hz, fc_hz * OVERLAP_OCTAVE_RATIO)
    )
    if not (w_lo_bound < fc_hz <= w_hi_bound):
        raise ValueError(
            f"woofer span [{w_lo_bound}, {w_hi_bound}] does not reach Fc={fc_hz}"
        )
    if not (t_lo_bound <= fc_hz < t_hi_bound):
        raise ValueError(
            f"tweeter span [{t_lo_bound}, {t_hi_bound}] does not reach Fc={fc_hz}"
        )
    # ratio > 1 is guaranteed by the two checks above (both quotients exceed 1).
    ratio = min(OVERLAP_OCTAVE_RATIO, fc_hz / w_lo_bound, t_hi_bound / fc_hz)
    return (fc_hz / ratio, fc_hz), (fc_hz, fc_hz * ratio)


def solve_branch_trims(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    fc_hz: float,
    *,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Level-match trims: each branch read on ITS OWN side of Fc.

    THIS IS THE LEVEL FACT: "level-matched" means matched acoustic output
    through the HANDOVER REGION — after the target filters, the two driver
    traces are equal at Fc and each sits -6 dB against the summed target
    (the Linkwitz-Riley unity condition). The linear-frequency power mean
    over the mirrored +/-1-octave halves IS that consensus statistic —
    NOT passband-average sensitivity
    (:func:`~jasper.active_speaker.linearization_fit.driver_core_level_db`
    is a different, legitimately-differing starting estimate for horn
    attenuation, disclosed rather than reconciled).

    Two constraints on the statistic: with LR4 the level-error sensitivity
    concentrates AT Fc, so +/-1-octave is right; and where woofer/horn
    directivity mismatch, no single correct level exists, so the axis
    levels were read on is stated (``intervention.LEVEL_MATCH_AXIS``).

    Two constraints on the statistic. *Slope:* with LR4 the sensitivity to
    level error concentrates AT Fc, so the ±1-octave matching band is right and
    the null test carries the weight; a shallower crossover would widen it
    toward ±1.5 octaves. *Directivity (Toole):* where woofer beaming and horn
    directivity mismatch, the on-axis, listening-window and power-response
    ratios differ and there is no single correct level — which is why the axis
    these levels were read on is stated rather than assumed
    (``active_speaker.profile.LEVEL_MATCH_AXIS``).

    Public as the level match's SSOT — the v2 session and contract tests
    import it.
    """
    (w_lo, w_hi), (t_lo, t_hi) = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    level_w = _band_average_db(
        freqs, 20.0 * np.log10(np.maximum(np.abs(W), 1e-12)), w_lo, w_hi
    )
    level_t = _band_average_db(
        freqs, 20.0 * np.log10(np.maximum(np.abs(T), 1e-12)), t_lo, t_hi
    )
    target = min(level_w, level_t)  # attenuate the louder branch
    return target - level_w, target - level_t, level_w, level_t


@dataclass(frozen=True)
class RealizedLevelMatch:
    """What the two branches ACTUALLY hand off at, once the trim is applied.

    ``difference_db`` is ``level_t_db - level_w_db``: the signed inter-driver
    level error, whose design intent is **zero**. Sign is kept because "the
    tweeter is 9 dB down" and "the tweeter is 9 dB up" are opposite defects and
    a bare magnitude hides which one shipped.

    ``matched`` is ``abs(difference_db) <= tolerance_db``. It is the verdict; it
    is not advice.
    """

    level_w_db: float
    level_t_db: float
    difference_db: float
    tolerance_db: float
    matched: bool
    woofer_band_hz: tuple[float, float]
    tweeter_band_hz: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_w_db": self.level_w_db,
            "level_t_db": self.level_t_db,
            "difference_db": self.difference_db,
            "tolerance_db": self.tolerance_db,
            "matched": self.matched,
            "woofer_band_hz": list(self.woofer_band_hz),
            "tweeter_band_hz": list(self.tweeter_band_hz),
        }


def realized_branch_level_match(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    fc_hz: float,
    *,
    trim_w_db: float,
    trim_t_db: float,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
    tolerance_db: float = REALIZED_LEVEL_MATCH_TOLERANCE_DB,
) -> RealizedLevelMatch:
    """Each branch's REALIZED power-band level, on its own side of Fc, after
    the committed trim — and whether the two agree.

    The assertion nothing else in the chain makes: no other comparator
    compares the two drivers' realized handoff levels to each other (the
    gap a ~9 dB-dark tweeter walked through). ``W``/``T`` are the branch
    transfer functions AS EMITTED (linearized on the v2 path);
    ``trim_*_db`` are applied here and re-read, grading the committed
    decision rather than re-litigating it.

    Reuses :func:`solve_branch_trims`'s own estimator (same bands, same
    average) rather than a rival check, inheriting its known +0.54 dB
    systematic rather than adding an unknown one. Raises ``ValueError``
    through the same surface as the trim solve it wraps — never a guessed
    verdict.
    """
    g_w = 10.0 ** (float(trim_w_db) / 20.0)
    g_t = 10.0 ** (float(trim_t_db) / 20.0)
    _residual_w, _residual_t, level_w, level_t = solve_branch_trims(
        freqs, W * g_w, T * g_t, fc_hz,
        woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    woofer_band, tweeter_band = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    difference = float(level_t - level_w)
    return RealizedLevelMatch(
        level_w_db=float(level_w),
        level_t_db=float(level_t),
        difference_db=difference,
        tolerance_db=float(tolerance_db),
        matched=abs(difference) <= float(tolerance_db),
        woofer_band_hz=woofer_band,
        tweeter_band_hz=tweeter_band,
    )


def ripple_at_trim(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    *,
    lo_hz: float,
    hi_hz: float,
    trim_w_db: float,
    trim_t_db: float,
    sign: int,
) -> float:
    """The summed pair's ripple (max-min dB) over ``[lo_hz, hi_hz]`` at ONE trim.

    The shared computation behind the trim-rejection telemetry PAIR:
    :func:`solve_ripple_optimal_trim` evaluates every scanned candidate
    through it, and the linearization planner calls it once more at the
    ANCHORED trim, so the two logged ripples differ in exactly one
    variable. Not this module's only summed-ripple site —
    :func:`_select_alignment_pair` asks a different question (which pair
    sums flatter at a fixed trim) and scores at the band-average trim to
    avoid circularity. No delay term: this is the zero-residual frame the
    scan optimizes in. ``inf`` when the band holds no bins.
    """
    return _ripple_db(
        freqs,
        predicted_branch_sum(
            w_tf, t_tf, float(trim_w_db), float(trim_t_db), int(sign),
        ),
        float(lo_hz),
        float(hi_hz),
    )


def solve_ripple_optimal_trim(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    fc_hz: float,
    *,
    lo_hz: float | None = None,
    hi_hz: float | None = None,
    seed_trim_db: float,
    trim_w_db: float = 0.0,
    sign: int,
    window_db: float = RIPPLE_TRIM_SEARCH_WINDOW_DB,
    step_db: float = RIPPLE_TRIM_SEARCH_STEP_DB,
    flat_minimum_epsilon_db: float = RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB,
) -> tuple[float, float, float]:
    """Ripple-minimizing tweeter trim, scanned around the band-average seed
    and regularized toward that seed on a flat minimum.

    A flatness POLISH on an already-correct level (``_build_candidate``
    runs it only where the band straddles Fc). The woofer trim
    (``trim_w_db``) is held FIXED at 0.0 by default — ripple depends only
    on RELATIVE gain, so scanning one side explores the full space.

    Search window: ``seed_trim_db +/- window_db`` at ``step_db`` steps
    (default +/-10 dB / 0.1 dB), clamped to
    [:data:`RIPPLE_TRIM_MIN_DB`, :data:`RIPPLE_TRIM_MAX_DB`] so the scan
    never evaluates an unphysical candidate.

    Selection is flat-minimum-regularized: among candidates within
    ``flat_minimum_epsilon_db`` of the global minimum, the one closest to
    the seed wins — trading inaudible flatness for session-to-session
    repeatability on a shallow bowl.

    Returns ``(trim_t_db, ripple_db, seed_trim_db)``. ``lo_hz``/``hi_hz``
    default to Fc +/- 1 octave; every caller passes its own gating-clamped
    band explicitly.
    """
    lo = lo_hz if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
    hi = hi_hz if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
    band = (freqs >= lo) & (freqs <= hi)
    if not np.any(band):
        raise ValueError("overlap band has no frequency bins")
    freqs_band = freqs[band]
    w_band = w_tf[band]
    t_band = t_tf[band]

    n_steps = int(round(window_db / step_db))
    raw_candidates = [seed_trim_db + i * step_db for i in range(-n_steps, n_steps + 1)]
    candidate_trims = [
        trim for trim in raw_candidates if RIPPLE_TRIM_MIN_DB <= trim <= RIPPLE_TRIM_MAX_DB
    ]
    if not candidate_trims:
        # No physically valid value in the seed's own window: clamp the seed itself.
        candidate_trims = [min(max(seed_trim_db, RIPPLE_TRIM_MIN_DB), RIPPLE_TRIM_MAX_DB)]
    ripples_db = [
        ripple_at_trim(
            freqs_band,
            w_band,
            t_band,
            lo_hz=lo,
            hi_hz=hi,
            trim_w_db=trim_w_db,
            trim_t_db=candidate_trim,
            sign=sign,
        )
        for candidate_trim in candidate_trims
    ]

    min_ripple = min(ripples_db)
    best_trim = seed_trim_db
    best_ripple = min_ripple
    best_distance = math.inf
    for candidate_trim, ripple in zip(candidate_trims, ripples_db):
        # Flat-minimum regularization: closest-to-seed within epsilon of the
        # global minimum (see docstring); a sharp minimum has only one such candidate.
        if ripple > min_ripple + flat_minimum_epsilon_db:
            continue
        distance = abs(candidate_trim - seed_trim_db)
        if distance < best_distance:
            best_distance = distance
            best_trim = candidate_trim
            best_ripple = ripple
    return best_trim, best_ripple, seed_trim_db


def _n_fft_for(*irs: np.ndarray) -> int:
    longest = max(ir.size for ir in irs)
    return max(8192, 1 << (max(longest, 1) - 1).bit_length())


# --------------------------------------------------------------------------- #
# CHECK helpers
# --------------------------------------------------------------------------- #


# How much of the scheduled ambient window must actually be present in the
# capture for it to count as evidence. A capture that started late clips the
# window's HEAD (never its tail — the pilots follow it), and RMS is
# length-independent, so a shortened window is still an honest floor estimate;
# what this rejects is the degenerate case where a couple of hundred samples
# survive and the estimate is noise about noise. Below the fraction the caller
# gets ``None`` and the analysis degrades to "no ambient evidence, trust the
# pilots" — never to a fabricated floor.
#
# ONE policy, both windows: CHECK's 12 s session-ambient window
# (`_ambient_from_capture`) and MEASURE/VERIFY's 1 s pilot-ambient window
# (`_pilot_ambient_samples`) ask the same question of the same kind of
# evidence, so they share this constant rather than each carrying a number that
# can drift from the other.
AMBIENT_MIN_USABLE_FRACTION = 0.5


def _ambient_from_capture(
    capture: np.ndarray, sample_rate: int, ambient_seg: ProgramSegment, global_offset: int
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """CHECK's session-ambient window and its band-floor report.

    The window is CLIPPED to the capture, never SLID along it: ``end`` is
    computed from the window's own (possibly negative) schedule position,
    not the clamped start — sliding forward would read whatever the
    schedule put AFTER the window (on the shipped CHECK program, the
    courtesy beep) as if it were room floor, 39.5 dB hot on a 0.6 s late
    start. That number feeds BOTH `_snr_floor_ok` and `_solve_gain_plan`.

    Below :data:`AMBIENT_MIN_USABLE_FRACTION` of the window this degrades
    the same honest way `_pilot_ambient_samples` does: ``None`` samples
    plus an EMPTY band report (fail-closed, never a fabricated floor).
    """
    begin = global_offset + ambient_seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + ambient_seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * ambient_seg.n_samples:
        # Never a silent degrade: distinguishes "the room was quiet" from
        # "we never heard the room", which costs a commissioning attempt.
        log_event(
            logger,
            "program_analysis.ambient_window_unusable",
            level=logging.WARNING,
            scheduled_samples=int(ambient_seg.n_samples),
            surviving_samples=int(max(0, end - start)),
            capture_late_samples=int(max(0, -begin)),
        )
        empty = np.empty(0, dtype=np.float64)
        return None, snr_policy.framed_ambient_band_report(empty, sample_rate, percentile=95)
    samples = capture[start:end]
    return samples, snr_policy.framed_ambient_band_report(samples, sample_rate, percentile=95)


def _pilot_ambient_samples(
    program: ExcitationProgram, capture: np.ndarray, global_offset: int,
) -> np.ndarray | None:
    """The program's own room-listening window, or ``None`` if it has none.

    MEASURE/VERIFY programs carry an
    :data:`~jasper.audio_measurement.program.AMBIENT_SEGMENT_ID` window
    ahead of their leading pilot pair so `_pilot_observations`' in-band SNR
    guard has something to measure against; without it the guard's input
    is ``+inf`` and can never fire.

    Located by SCHEDULE offset, not correlation (it is silence). Clipped to
    the capture, never slid along it, sharing :data:`AMBIENT_MIN_USABLE_FRACTION`
    with `_ambient_from_capture`. Replay failure direction is safe: a
    too-loud "ambient" reads as low SNR, resolving ``linearity_ok`` to
    ``None`` rather than a false AGC accusation.
    """
    try:
        seg = program.segment(AMBIENT_SEGMENT_ID)
    except KeyError:
        return None
    begin = global_offset + seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * seg.n_samples:
        return None
    return capture[start:end]


def _band_power(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """Mean-square (linear power) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Hann-windowed before :func:`_bandlimit`'s zero-phase FFT bandpass: a raw
    slice rarely starts/ends at a zero crossing, so an un-windowed
    brick-wall filter leaks broadband energy from that boundary into every
    band. The Hann taper's constant windowing loss cancels out of every
    comparison that reads both sides through this same function.

    Returned as LINEAR power (not dB) so a caller can SUBTRACT an ambient
    noise-power estimate before converting to dB.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8:
        return 0.0
    filtered = _bandlimit(x * np.hanning(x.size), sample_rate, f1_hz, f2_hz)
    return float(np.mean(np.square(filtered)))


def _band_rms_dbfs(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """RMS level (dBFS) of ``samples`` restricted to ``[f1_hz, f2_hz]``.
    Thin dB wrapper over :func:`_band_power`."""
    power = _band_power(samples, sample_rate, f1_hz, f2_hz)
    if power <= 0 or not math.isfinite(power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(power))


def _pilot_trim_fade(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Drop the composer's fixed edge fade (`PILOT_FADE_TRIM_S`) from a
    located pilot segment. Falls back to the untrimmed segment when
    trimming would leave nothing; downstream SNR/linearity gates still
    catch a genuinely bad capture.
    """
    trim = int(round(PILOT_FADE_TRIM_S * sample_rate))
    if samples.size <= 2 * trim:
        return samples
    return samples[trim:-trim]


def _ambient_subtracted_dbfs(power: float, ambient_power: float) -> float:
    """dB of ``power`` after subtracting ``ambient_power`` (power domain).
    ``ambient_power`` is 0.0 with no ambient evidence (see
    `_pilot_ambient_samples`), degrading to plain in-band RMS.
    """
    signal_power = power - ambient_power if ambient_power > 0 else power
    if signal_power <= 0 or not math.isfinite(signal_power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(signal_power))


def _pilot_in_band_snr_db(power: float, ambient_power: float) -> float:
    """SNR (dB) of the ambient-subtracted estimate: ``(P - N) / N`` = ``S / N``
    in the ``P = S + N`` model, the linear SNR `PILOT_MIN_SNR_DB` is stated
    in. ``+inf`` with no ambient evidence; ``-inf`` when measured power
    does not even exceed ambient.
    """
    if ambient_power <= 0 or not math.isfinite(ambient_power):
        return math.inf
    ratio = power / ambient_power - 1.0
    if ratio <= 0 or not math.isfinite(ratio):
        return -math.inf
    return 10.0 * math.log10(ratio)


def _band_exclusive_pieces(
    other_band: tuple[float, float], own_band: tuple[float, float]
) -> list[tuple[float, float]]:
    """The part(s) of ``other_band`` that fall OUTSIDE ``own_band``.

    Declared bands legitimately overlap around the crossover point (design
    §5.2/§5.4), and that shared part carries no map-discrimination signal —
    the CROSS test (`_channel_map_ok`) only asks about the EXCLUSIVE
    remainder (interval subtraction; 0, 1, or 2 pieces).
    """
    o1, o2 = other_band
    a1, a2 = own_band
    pieces: list[tuple[float, float]] = []
    if o1 < a1:
        pieces.append((o1, min(o2, a1)))
    if o2 > a2:
        pieces.append((max(o1, a2), o2))
    return [(lo, hi) for lo, hi in pieces if hi > lo]


def _pilot_observations(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    ambient_samples: np.ndarray | None = None,
    channel_map_ambient_samples: np.ndarray | None = None,
) -> list[PilotObservation]:
    """Per-role pilot level/linearity/channel-map observations (design §3.4).

    Level is measured band-relative (each pilot's OWN declared band via
    `_band_power`) and, when an ambient window is available,
    ambient-power-subtracted before converting to dB — a full-band PEAK
    estimate would let LF room rumble inflate the quiet pilot's level and
    compress the captured delta. With no window (``ambient_samples=None``)
    subtraction is a no-op and SNR is trusted unconditionally.

    Two ambient parameters: ``ambient_samples`` feeds level/SNR;
    ``channel_map_ambient_samples`` feeds `_channel_map_ok`'s TARGET/CROSS
    rise test. CHECK passes the same 12 s window to both; MEASURE/VERIFY
    pass only the first, since their ~1 s pre-pilot window was never
    calibrated for the rise thresholds — their channel-map check keeps the
    total-in-band-energy-fraction fallback instead.

    The composer's fixed edge fade (`_pilot_trim_fade`) is trimmed before
    measuring so the RMS rides the steady-state portion, not the ramp.

    Low-SNR honest routing: the quiet (lo) pilot's in-band SNR
    (`_pilot_in_band_snr_db`) gates trust. Below `PILOT_MIN_SNR_DB`,
    ``linearity_ok`` is ``None`` (UNKNOWN, never a false failure or pass)
    and ``snr_valid=False`` routes to the honest room/positioning reason
    instead of blaming the phone's AGC.

    ``peak_lo_dbfs``/``peak_hi_dbfs`` are a SEPARATE, non-ambient-subtracted
    full-band `_peak_dbfs`: `_solve_gain_plan` uses a pilot level
    ABSOLUTELY, so an ambient-subtracted level would shift that reference
    by however much power was subtracted (measured 13-17 dB on real
    captures).
    """
    by_id = {loc.segment_id: loc for loc in locations}
    roles = sorted({seg.role for seg in program.segments if seg.kind == KIND_PILOT and seg.role})
    # Every role's declared band, for the CROSS test's "did energy also
    # rise in every OTHER role's band" question.
    role_bands: dict[str, tuple[float, float]] = {}
    for role in roles:
        hi_seg = program.segment(f"pilot_{role}_hi")
        if hi_seg.f1_hz is None or hi_seg.f2_hz is None:
            raise ValueError(f"pilot segment for role {role!r} has no declared band")
        role_bands[role] = (hi_seg.f1_hz, hi_seg.f2_hz)

    ambient_arr = None if ambient_samples is None else np.asarray(ambient_samples)
    if ambient_arr is not None and ambient_arr.size < 8:
        ambient_arr = None
    has_ambient = ambient_arr is not None

    out: list[PilotObservation] = []
    for role in roles:
        lo_seg = program.segment(f"pilot_{role}_lo")
        hi_seg = program.segment(f"pilot_{role}_hi")
        lo_loc = by_id[f"pilot_{role}_lo"]
        hi_loc = by_id[f"pilot_{role}_hi"]
        lo_samples = capture[lo_loc.located_start:lo_loc.located_start + lo_seg.n_samples]
        hi_samples = capture[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]

        own_f1, own_f2 = role_bands[role]
        lo_interior = _pilot_trim_fade(lo_samples, sample_rate)
        hi_interior = _pilot_trim_fade(hi_samples, sample_rate)
        lo_power = _band_power(lo_interior, sample_rate, own_f1, own_f2)
        hi_power = _band_power(hi_interior, sample_rate, own_f1, own_f2)
        ambient_power = (
            _band_power(ambient_arr, sample_rate, own_f1, own_f2)
            if ambient_arr is not None
            else 0.0
        )

        level_lo = _ambient_subtracted_dbfs(lo_power, ambient_power)
        level_hi = _ambient_subtracted_dbfs(hi_power, ambient_power)
        programmed_delta = hi_seg.gain_db - lo_seg.gain_db
        captured_delta = level_hi - level_lo

        lo_snr_db = _pilot_in_band_snr_db(lo_power, ambient_power) if has_ambient else math.inf
        snr_valid = lo_snr_db >= PILOT_MIN_SNR_DB
        # UNKNOWN below the SNR floor, never True: the captured delta is not
        # evidence in EITHER direction down there.
        linearity_ok = (
            None if not snr_valid
            else abs(captured_delta - programmed_delta) <= LINEARITY_TOLERANCE_DB
        )

        # Gain-solve reference: full-band peak, NOT the ambient-subtracted level.
        peak_lo = _peak_dbfs(lo_samples)
        peak_hi = _peak_dbfs(hi_samples)

        own_band = role_bands[role]
        other_bands = tuple(
            piece
            for other_role, other_band in role_bands.items()
            if other_role != role
            for piece in _band_exclusive_pieces(other_band, own_band)
        )
        channel_ok, channel_target_rise_db, channel_cross_rise_db = _channel_map_ok(
            hi_samples, sample_rate, hi_seg,
            ambient_samples=channel_map_ambient_samples, other_bands=other_bands,
        )
        out.append(PilotObservation(
            role=role,
            level_lo_dbfs=level_lo,
            level_hi_dbfs=level_hi,
            programmed_delta_db=programmed_delta,
            captured_delta_db=captured_delta,
            linearity_ok=linearity_ok,
            channel_map_ok=channel_ok,
            snr_valid=snr_valid,
            peak_lo_dbfs=peak_lo,
            peak_hi_dbfs=peak_hi,
            snr_db=lo_snr_db,
            channel_map_target_rise_db=channel_target_rise_db,
            channel_map_cross_rise_db=channel_cross_rise_db,
            programmed_hi_gain_db=hi_seg.gain_db,
        ))
    return out


def _aggregate_tri_state_ok(
    verdicts: Sequence[bool | None],
) -> bool | None:
    """Reduce per-role tri-state verdicts to one, FAILURE-dominant.

    A FAILURE anywhere is the verdict; otherwise an UNKNOWN anywhere makes
    the whole verdict unknown ("the roles we could read were fine" is not
    "every role was fine"). ``None`` for no roles at all. Written out
    rather than ``all(...)``, which folds ``None`` to False — for
    ``channel_map_ok`` that would be a hard stop on evidence never there.
    """
    if not verdicts:
        return None
    if any(v is False for v in verdicts):
        return False
    if any(v is None for v in verdicts):
        return None
    return True


def _aggregate_linearity_ok(
    pilots: Sequence[PilotObservation],
) -> bool | None:
    """Per-pilot ``linearity_ok`` over the roles, through the shared fold."""
    return _aggregate_tri_state_ok([p.linearity_ok for p in pilots])


def _pilot_verdicts(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    global_offset: int,
) -> tuple[tuple[PilotObservation, ...], bool | None, bool | None, bool | None]:
    """Pilot observations + the aggregate linearity/channel-map/SNR verdicts.

    ``None`` verdicts when the program carries no pilots, so a caller can
    distinguish "no pilot evidence" from "pilot evidence, all clean".
    Shared by v2 MEASURE/VERIFY, whose leading pilot pair (design §5.2)
    reads its own pre-pilot ambient window so ``pilot_snr_ok`` is a real
    verdict; the channel-map check still uses
    `_channel_map_ok`'s one-sided fallback (see `_pilot_observations`).
    """
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=_pilot_ambient_samples(program, capture, global_offset),
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    return tuple(pilots), linearity_ok, channel_map_ok, pilot_snr_ok


def _channel_map_ok(
    samples: np.ndarray,
    sample_rate: int,
    seg: ProgramSegment,
    *,
    ambient_samples: np.ndarray | None = None,
    other_bands: Sequence[tuple[float, float]] = (),
) -> tuple[bool | None, float | None, float | None]:
    """Band-relative channel-map sanity (design note above `CHANNEL_MAP_*`).

    Given an ambient window, asks two independent questions per pilot
    rather than a single total-energy fraction a concurrent unrelated
    room-noise band can veto:

    1. TARGET: did THIS driver's own declared band rise
       ``CHANNEL_MAP_TARGET_RISE_DB`` above that band's ambient level?
    2. CROSS: did every OTHER driver's band stay at least
       ``CHANNEL_MAP_MIN_ISOLATION_DB`` below this driver's own rise (the
       ISOLATION RATIO)? Guards ABNORMAL CROSS-BAND ENERGY (bleed, skirt,
       nonlinearity) — not the mis-wire discriminator, which rung 1
       catches. A ratio rather than an additive bound because honest
       cross-band content sits at a roughly fixed RELATIVE level (see
       ``CHANNEL_MAP_MIN_ISOLATION_DB``'s derivation). Judged only above
       ``CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB``, since below it the CROSS
       test would turn a retriable ``snr_floor`` into a rewire hard stop.

    Without an ambient window, falls back to the fraction test (energy in
    the declared band must exceed half the pilot window's total spectral
    energy) — the path v2 MEASURE/VERIFY and a windowless program take.
    That fallback is ONE-SIDED: a cleared fraction is ``None`` (UNKNOWN,
    since broadband room noise clears it too); a failed fraction keeps its
    ``False``.

    Returns ``(ok, target_rise_db, cross_rise_db)`` — the two RAW rise
    numbers, so an operator can see WHICH half moved (the ratio is derived
    by `channel_map_isolation_db`). ``cross_rise_db`` is the rise that
    failed CROSS when ``ok`` is False, or the worst rise observed when
    ``ok`` is True; both are ``None`` on the fallback path or with no
    ``other_bands``.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8 or seg.f1_hz is None or seg.f2_hz is None:
        return False, None, None

    if ambient_samples is None or np.asarray(ambient_samples).size < 8:
        window = np.hanning(x.size)
        spectrum = np.abs(np.fft.rfft(x * window)) ** 2
        freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
        in_band = (freqs >= seg.f1_hz) & (freqs <= seg.f2_hz)
        total = float(np.sum(spectrum))
        if total <= 0:
            return False, None, None
        # One-sided: the fail is a finding, the pass is not evidence.
        if float(np.sum(spectrum[in_band])) / total > 0.5:
            return None, None, None
        return False, None, None

    target_rise = (
        _band_rms_dbfs(x, sample_rate, seg.f1_hz, seg.f2_hz)
        - _band_rms_dbfs(ambient_samples, sample_rate, seg.f1_hz, seg.f2_hz)
    )
    if target_rise < CHANNEL_MAP_TARGET_RISE_DB:
        return False, target_rise, None
    # Cross rises are always MEASURED; only JUDGED above CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB.
    judge_cross = target_rise >= CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
    worst_cross_rise: float | None = None
    for other_f1, other_f2 in other_bands:
        cross_rise = (
            _band_rms_dbfs(x, sample_rate, other_f1, other_f2)
            - _band_rms_dbfs(ambient_samples, sample_rate, other_f1, other_f2)
        )
        if worst_cross_rise is None or cross_rise > worst_cross_rise:
            worst_cross_rise = cross_rise
        if not judge_cross:
            continue
        isolation = channel_map_isolation_db(target_rise, cross_rise)
        # Fail-closed: an unjudgeable ratio must never read as a PASS.
        if isolation is None or isolation < CHANNEL_MAP_MIN_ISOLATION_DB:
            return False, target_rise, cross_rise
    return True, target_rise, worst_cross_rise


def channel_map_isolation_db(
    target_rise_db: float | None, cross_rise_db: float | None
) -> float | None:
    """The channel-map ISOLATION RATIO: this driver's rise minus the cross
    rise. ONE definition, read by both `_channel_map_ok`'s decision and
    every reporting surface. ``None`` whenever either rise is absent — a
    caller must treat that as "no evidence", never a pass.
    """
    if target_rise_db is None or cross_rise_db is None:
        return None
    return target_rise_db - cross_rise_db


def _bands_overlap(
    lo_a: float, hi_a: float, lo_b: float, hi_b: float
) -> bool:
    return hi_a > lo_b and lo_a < hi_b


def _ambient_rows_in_band(
    band_hz: tuple[float, float],
    ambient_bands: Sequence[Any],
) -> list[tuple[float, float, float]]:
    """The ``(lo_hz, hi_hz, level_dbfs)`` ambient rows overlapping ``band_hz``.
    A row this cannot read is skipped rather than raised on, never crashing
    inside CHECK's accept path.
    """
    lo, hi = band_hz
    rows: list[tuple[float, float, float]] = []
    for entry in ambient_bands or ():
        if not isinstance(entry, Mapping):
            continue
        edges = entry.get("band_hz")
        if not (isinstance(edges, (list, tuple)) and len(edges) == 2):
            continue
        try:
            b_lo, b_hi, level = (
                float(edges[0]), float(edges[1]), float(entry["level_dbfs"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(b_lo) and math.isfinite(b_hi) and math.isfinite(level)):
            continue
        if _bands_overlap(lo, hi, b_lo, b_hi):
            rows.append((b_lo, b_hi, level))
    return rows


def _band_required_snr_db(
    lo_hz: float, hi_hz: float, overlap_hz: tuple[float, float] | None
) -> float:
    """The SNR the fit needs in one band, per the split SNR policy.

    ``jasper.audio_measurement.snr_policy`` splits SNR trust by what a number
    is used FOR: a magnitude/trim decision is usable at
    ``DRIVER.snr_ok_db``, while a null/alignment decision (MEASURE's GCC
    delay + polarity estimate, which reads the crossover overlap band) needs
    ``DRIVER.alignment_snr_ok_db``. A band inside the overlap window carries
    the alignment requirement; every other band carries the magnitude one.

    ``overlap_hz`` is ``None`` when no Fc prior reached this analysis. That
    resolves to the alignment requirement EVERYWHERE — the conservative
    direction, since a higher requirement means a LOUDER solve. An unknown Fc
    must never buy a quieter measurement.
    """
    if overlap_hz is None or _bands_overlap(
        lo_hz, hi_hz, overlap_hz[0], overlap_hz[1]
    ):
        return DRIVER.alignment_snr_ok_db
    return DRIVER.snr_ok_db


def _solve_role_gain(
    *,
    role: str,
    k_db: float,
    flat_target_gain_db: float,
    band_hz: tuple[float, float] | None,
    pilot_delta_db: float,
    ambient_bands: Sequence[Any],
    overlap_hz: tuple[float, float] | None,
) -> RoleGainSolve:
    """The quietest MEASURE gain for one driver that still serves the fit.

    ``k_db`` is this driver's measured chain gain (captured peak minus the
    digital gain that produced it), so a target capture peak ``C`` is
    reached at digital gain ``C - k_db``. Three floors compete and the
    LOUDEST wins. Every arm is peak-expressed via
    :func:`sweep_band_crest_factor_db`, since ambient levels and SNR
    requirements are RMS.

    * **room SNR** — worst ``ambient + required_snr`` across ambient bands
      overlapping this driver's own measurement band (band-scoped so a
      tweeter needs less drive than a woofer). The ambient table
      (``snr_policy.CROSSOVER_SNR_BANDS_HZ``) is coarse in two known ways,
      both erring LOUD: wide rows near a sweep's edge inherit the row's
      full level, and the table stops at 12 kHz (room noise there is below
      every lower band anyway).
    * **pilot SNR** — MEASURE's leading pilot pair fails when its quiet
      side's in-band SNR falls under ``PILOT_MIN_SNR_DB``; applied to every
      role as a floor so it stays correct even if the composer moves the pair.
    * **capture floor** — ``DRIVER.peak_too_low_dbfs``, a TRIPWIRE not a
      shippable bound: if it wins, both other arms have resolved below an
      unmeasurable level, so the solve is REFUSED (falls back to
      ``flat_target_gain_db`` with ``bound_by=GAIN_BOUND_DEGENERATE_AMBIENT``
      and a WARNING) rather than shipping a level that once solved a real
      driver to -45 dBFS, 34 dB below the flat level.

    The result is clamped by ``flat_target_gain_db``: this solve can only
    make MEASURE quieter than the level-only figure, never louder.
    """
    rows = _ambient_rows_in_band(band_hz, ambient_bands) if band_hz else []
    if not rows:
        # Disclosed fallback: no ambient evidence to solve against.
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_NO_AMBIENT_EVIDENCE,
            band_hz=band_hz,
        )

    demands: list[tuple[float, float, float, float]] = []
    for lo, hi, level in rows:
        required_snr = (
            _band_required_snr_db(lo, hi, overlap_hz) + MEASURE_SNR_SOLVE_MARGIN_DB
        )
        # level + required_snr is band-RMS; carry the crest factor so both
        # sides of the eventual peak comparison are peak-expressed.
        crest = sweep_band_crest_factor_db(band_hz, (lo, hi)) if band_hz else 0.0
        demands.append((level + required_snr + crest, level, required_snr, crest))
    required_capture_dbfs, ambient_dbfs, required_snr_db, crest_factor_db = max(
        demands, key=lambda item: item[0]
    )
    # Named residual, erring QUIET: built from the single worst overlapping
    # ROW rather than the pilot's whole band. Not the binding arm anywhere
    # measured (JTS3: room arm wins by 16-19 dB).
    worst_ambient_dbfs = max(level for _lo, _hi, level in rows)
    pilot_floor_dbfs = (
        worst_ambient_dbfs + pilot_delta_db + PILOT_MIN_SNR_DB
        + MEASURE_SNR_SOLVE_MARGIN_DB
        + SWEEP_PEAK_TO_RMS_DB
    )
    capture_dbfs, bound_by = max(
        (
            (required_capture_dbfs, GAIN_BOUND_ROOM_SNR),
            (pilot_floor_dbfs, GAIN_BOUND_PILOT_SNR),
            (DRIVER.peak_too_low_dbfs, GAIN_BOUND_CAPTURE_FLOOR),
        ),
        key=lambda item: item[0],
    )
    if bound_by == GAIN_BOUND_CAPTURE_FLOOR:
        # A floor-bound solve is not a level, it is evidence the ambient
        # report cannot be solved against — refuse, keep the flat target.
        log_event(
            logger,
            "program_analysis.measure_level_solve_refused",
            level=logging.WARNING,
            role=role,
            reason=GAIN_BOUND_DEGENERATE_AMBIENT,
            capture_floor_dbfs=round(DRIVER.peak_too_low_dbfs, 2),
            required_capture_dbfs=round(required_capture_dbfs, 2),
            pilot_floor_dbfs=round(pilot_floor_dbfs, 2),
            ambient_dbfs=round(ambient_dbfs, 2),
            fallback_gain_db=round(flat_target_gain_db, 3),
        )
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_DEGENERATE_AMBIENT,
            band_hz=band_hz,
            # Retained deliberately: what the ambient report claimed, not just that it was rejected.
            ambient_dbfs=ambient_dbfs,
            required_snr_db=required_snr_db,
            required_capture_dbfs=required_capture_dbfs,
            crest_factor_db=crest_factor_db,
        )
    gain_db = capture_dbfs - k_db
    if gain_db >= flat_target_gain_db:
        gain_db, bound_by = flat_target_gain_db, GAIN_BOUND_FLAT_TARGET
    return RoleGainSolve(
        role=role,
        gain_db=gain_db,
        flat_target_gain_db=flat_target_gain_db,
        bound_by=bound_by,
        band_hz=band_hz,
        ambient_dbfs=ambient_dbfs,
        required_snr_db=required_snr_db,
        required_capture_dbfs=required_capture_dbfs,
        crest_factor_db=crest_factor_db,
    )


def _solve_gain_plan(
    program: ExcitationProgram,
    pilots: Sequence[PilotObservation],
    ambient_report: Mapping[str, Any],
    priors: MeasurementPriors,
) -> GainPlan:
    target = priors.target_capture_dbfs
    ambient_bands = (
        ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    ) or ()
    # The nominal Fc +/- 1 octave window, UNCLAMPED: a narrower band would
    # buy a quieter solve on a technicality. Wider is the safe read here.
    overlap_hz = (
        overlap_band_hz(float(priors.crossover_fc_hz))
        if priors.crossover_fc_hz else None
    )
    gains: dict[str, float] = {}
    solves: dict[str, RoleGainSolve] = {}
    predicted_peaks: list[float] = []
    for pilot in pilots:
        lo_seg = program.segment(f"pilot_{pilot.role}_lo")
        hi_seg = program.segment(f"pilot_{pilot.role}_hi")
        # captured = digital_gain + K (unit slope). K from the two pilots,
        # deliberately the PEAK-referenced levels, not the ambient-subtracted ones.
        k_lo = pilot.peak_lo_dbfs - lo_seg.gain_db
        k_hi = pilot.peak_hi_dbfs - hi_seg.gain_db
        k = (k_lo + k_hi) / 2.0
        # The level-only answer, and the CEILING of the solve below.
        flat_gain = min(target - k, GAIN_MAX_DIGITAL_PEAK_DBFS)  # >=6 dB guard
        solve = _solve_role_gain(
            role=pilot.role,
            k_db=k,
            flat_target_gain_db=flat_gain,
            # A CHECK pilot's band IS the role's MEASURE sweep band.
            band_hz=(
                (float(lo_seg.f1_hz), float(lo_seg.f2_hz))
                if lo_seg.f1_hz is not None and lo_seg.f2_hz is not None
                else None
            ),
            pilot_delta_db=abs(hi_seg.gain_db - lo_seg.gain_db),
            ambient_bands=ambient_bands,
            overlap_hz=overlap_hz,
        )
        gains[pilot.role] = solve.gain_db
        solves[pilot.role] = solve
        predicted_peaks.append(solve.gain_db)
    predicted_peak = max(predicted_peaks) if predicted_peaks else GAIN_MAX_DIGITAL_PEAK_DBFS

    # Deliberately judged at `target_capture_dbfs`, NOT the solved level —
    # this is the room-quality gate ("is this room quiet enough at all"),
    # a different question from the per-driver solve above.
    snr_floor_ok = _snr_floor_ok(ambient_report, target)
    return GainPlan(
        gain_db=gains,
        predicted_peak_dbfs=predicted_peak,
        snr_floor_ok=snr_floor_ok,
        role_solves=solves,
    )


def _snr_floor_ok(ambient_report: Mapping[str, Any], target_capture_dbfs: float) -> bool:
    """False when the ambient report is missing, empty, or every row is
    unreadable — never raises on a malformed ``level_dbfs``.
    """
    bands = ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    if not bands:
        return False
    worst: float | None = None
    for b in bands:
        if not isinstance(b, Mapping):
            continue
        try:
            level = float(b["level_dbfs"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(level):
            continue
        if worst is None or level > worst:
            worst = level
    if worst is None:
        return False
    return (target_capture_dbfs - worst) >= DRIVER.snr_ok_db


# --------------------------------------------------------------------------- #
# phase dispatch
# --------------------------------------------------------------------------- #


def analyze_program_capture(
    program: ExcitationProgram,
    samples: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None" = None,
    geometry: MeasurementGeometry | None = None,
    priors: MeasurementPriors | None = None,
    capture_report: Mapping[str, Any] | None = None,
) -> ProgramAnalysis:
    """Analyze a program capture into a :class:`ProgramAnalysis` (design §5.6).

    ``capture_report`` is the capture page's own per-take account
    (``CaptureResult.capture_integrity``), reconciled against the frames
    actually handed in; the resulting :class:`~.frame_ledger.FrameLedger`
    rides every returned analysis. ``None`` leaves page-side counts
    unreported, grading as not-evaluated rather than loss.
    """
    if sample_rate != program.sample_rate_hz:
        raise ValueError(
            f"capture rate {sample_rate} != program rate {program.sample_rate_hz}"
        )
    capture = np.asarray(samples, dtype=np.float64).ravel()
    # BEFORE the truncation below, a legitimate transform that must never
    # read as loss.
    frame_ledger = reconcile_capture_frames(
        capture_report, received_frames=int(capture.size),
    )
    _log_frame_ledger(program, frame_ledger)
    # Bound the capture BEFORE any full-rate FFT (1 GB Pi).
    capture = deconv.cap_capture_length(
        capture,
        sweep_len=program.total_samples,
        sample_rate=sample_rate,
        max_capture_seconds=(
            program.total_samples / sample_rate + CAPTURE_BOUND_MARGIN_S
        ),
    )
    geometry = geometry or MeasurementGeometry()
    priors = priors or MeasurementPriors()

    global_offset, _first, stimuli, anchor_ambiguous = _global_offset(
        program, capture, sample_rate
    )
    locations = _locate_segments(program, capture, sample_rate, global_offset, stimuli)

    if program.phase == PROGRAM_PHASE_CHECK:
        analysis = _analyze_check(
            program, capture, sample_rate, global_offset, locations, priors,
        )
    elif program.phase == PROGRAM_PHASE_MEASURE:
        analysis = _analyze_measure(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors,
        )
    elif program.phase == PROGRAM_PHASE_VERIFY:
        analysis = _analyze_verify(
            program, capture, sample_rate, global_offset, locations,
            calibration, priors, frame_ledger,
        )
    else:
        raise ValueError(f"unknown phase: {program.phase!r}")
    # Attached HERE, one assignment site, so a phase that grows its own
    # analyzer later cannot ship without it.
    return replace(
        analysis, frame_ledger=frame_ledger, anchor_ambiguous=anchor_ambiguous
    )


def _analyze_check(
    program, capture, sample_rate, global_offset, locations, priors,
) -> ProgramAnalysis:
    ambient_seg = program.segment(AMBIENT_SEGMENT_ID)
    ambient_samples, ambient_report = _ambient_from_capture(
        capture, sample_rate, ambient_seg, global_offset
    )
    # CHECK's 12 s ambient window feeds both the level/SNR path and the
    # channel-map rise test (see `_pilot_observations`).
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=ambient_samples,
        channel_map_ambient_samples=ambient_samples,
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    gain_plan = _solve_gain_plan(program, pilots, ambient_report, priors)
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        ambient_report=ambient_report,
        pilots=tuple(pilots),
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        gain_plan=gain_plan,
    )


def _compose_configured_path_ir(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    radiated_band_hz: tuple[float, float] | None,
    priors: MeasurementPriors,
) -> np.ndarray:
    """Replace exact emitted protection ``P`` with configured crossover ``C``.

    ``S = M*C/P`` (design §4.2) as ONE filter across the whole rfft support,
    never spliced into a sub-band. The conditioning policy binds ONLY on
    §4.2's candidate-required bins (the DRIVEN band intersected with
    ``priors.candidate_required_band_hz_by_role``); outside them the same
    exact ratio applies, saturated at +12 dB. A segment declaring no sweep
    bounds is missing evidence, not ill-conditioning.
    """
    maps = (
        priors.measurement_protection_response_by_role,
        priors.configured_crossover_response_by_role,
        priors.configured_polarity_sign_by_role,
    )
    if all(value is None for value in maps):
        return full_ir
    if any(value is None for value in maps):
        raise ConfiguredPathConditioningError("incomplete priors")
    if radiated_band_hz is None:
        # Missing evidence, not ill-conditioning: no sweep bounds declared.
        raise ValueError(f"{role} sweep segment has no declared band")
    protection_by_role, configured_by_role, polarity_by_role = maps
    try:
        protection_response = protection_by_role[role]  # type: ignore[index]
        configured_response = configured_by_role[role]  # type: ignore[index]
        polarity_sign = polarity_by_role[role]  # type: ignore[index]
    except KeyError as exc:
        raise ConfiguredPathConditioningError(f"missing role {role}") from exc
    if type(polarity_sign) is not int or polarity_sign not in (-1, 1):
        raise ConfiguredPathConditioningError(f"invalid polarity for {role}")
    samples = np.asarray(full_ir, dtype=np.float64)
    measured = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    lo_hz, hi_hz = (float(value) for value in radiated_band_hz)
    band = (priors.candidate_required_band_hz_by_role or {}).get(role)
    if band is not None:
        lo_hz, hi_hz = max(lo_hz, float(band[0])), min(hi_hz, float(band[1]))
    required = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(required):
        raise ConfiguredPathConditioningError(f"no trusted bins for {role}")
    protection = np.asarray(protection_response(freqs), dtype=np.complex128)
    configured = np.asarray(configured_response(freqs), dtype=np.complex128)
    for label, response in (("P", protection), ("C", configured)):
        if not np.all(np.isfinite(response[required])):
            raise ConfiguredPathConditioningError(f"non-finite {label} for {role}")
    minimum = 10.0 ** (CONFIGURED_PATH_PROTECTION_FLOOR_DB / 20.0)
    if np.any(np.abs(protection[required]) < minimum):
        raise ConfiguredPathConditioningError(
            f"P below {CONFIGURED_PATH_PROTECTION_FLOOR_DB:g} dB for {role}",
            protection_floor=True,
        )
    ratio = np.divide(
        configured, protection,
        out=np.zeros_like(configured), where=protection != 0,
    )
    maximum = 10.0 ** (12.0 / 20.0)
    if not np.all(np.isfinite(ratio[required])) or np.any(
        np.abs(ratio[required]) > maximum
    ):
        raise ConfiguredPathConditioningError(f"C/P above +12 dB for {role}")
    saturated = ratio * (maximum / np.maximum(np.abs(ratio), maximum))
    spectrum = measured * polarity_sign * saturated
    if not np.all(np.isfinite(spectrum[required])):
        raise ConfiguredPathConditioningError(f"non-finite S for {role}")
    spectrum[~np.isfinite(spectrum)] = 0.0  # off-required, but rides the irfft
    return np.fft.irfft(spectrum, n=samples.size)


def _repeat_driver_responses(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    occurrences: Sequence[SegmentLocation],
    *,
    role: str,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
    priors: MeasurementPriors,
) -> tuple[DriverResponse, ...]:
    """Deconvolve + gate + TF every occurrence AFTER the first (design item 7).

    Per-repeat evidence, individually bounded exactly like the primary
    response. The PRIMARY response is built by the caller and untouched
    here; repeats never feed the candidate/trim/alignment math. Consumed
    by ``linearization_envelope.compute_sigma_curve`` as the Layer-1a
    repeatability term.
    """
    out: list[DriverResponse] = []
    for repeat_index, loc in enumerate(occurrences[1:], start=1):
        seg = program.segment(loc.segment_id)
        full_ir, _pre = _deconvolve_window(
            capture, seg, global_offset + seg.start_sample, sample_rate,
            epsilon=epsilon,
        )
        full_ir = _compose_configured_path_ir(
            role, full_ir, sample_rate, _radiated_band_hz(seg), priors
        )
        resp = _driver_response(
            role, full_ir, sample_rate,
            calibration=calibration, ambient_report=ambient_report,
            fc_hz=fc_hz, n_fft=n_fft,
            radiated_band_hz=_radiated_band_hz(seg),
            capture_segment=_raw_sweep_segment(
                capture, seg, global_offset + seg.start_sample,
            ),
        )
        out.append(replace(resp, repeat_index=repeat_index))
    return tuple(out)


#: :attr:`ProgramAnalysis.measure_pair_not_evaluated`'s only value today: one
#: driver, so no pair to align or to build a candidate across.
MEASURE_PAIR_SINGLE_DRIVER = "single_driver_no_pair"


def _analyze_measure(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors,
) -> ProgramAnalysis:
    # ``None`` is legal for exactly one shape: a 1-way passive main. On a
    # TWO-branch program it still raises below, where ``seg_t`` is known.
    fc_hz = None if priors.crossover_fc_hz is None else float(priors.crossover_fc_hz)
    drift = _estimate_drift(program, capture, sample_rate, locations)

    seg_w = program.segment("sweep_w")
    seg_t = next(
        (seg for seg in program.segments if seg.segment_id == "sweep_t"), None
    )
    if seg_t is not None and fc_hz is None:
        raise ValueError("MEASURE analysis requires priors.crossover_fc_hz")
    epsilon = drift.epsilon_ppm / 1e6
    # Deconvolve both sweeps anchored at their SCHEDULE window (with a shared
    # pre-guard) so relative timing survives (the aligner relies on this); the
    # measured ε is divided out of the reference so drift can't smear the IR.
    woofer_full_ir, pre_w = _deconvolve_window(
        capture, seg_w, global_offset + seg_w.start_sample, sample_rate,
        epsilon=epsilon,
    )
    woofer_full_ir = _compose_configured_path_ir(
        seg_w.role, woofer_full_ir, sample_rate, _radiated_band_hz(seg_w), priors
    )
    tweeter_full_ir = None
    pre_samples = pre_w
    if seg_t is not None:
        tweeter_full_ir, pre_t = _deconvolve_window(
            capture, seg_t, global_offset + seg_t.start_sample, sample_rate,
            epsilon=epsilon,
        )
        tweeter_full_ir = _compose_configured_path_ir(
            seg_t.role, tweeter_full_ir, sample_rate, _radiated_band_hz(seg_t), priors
        )
        pre_samples = min(pre_w, pre_t)
    n_fft = _n_fft_for(*[ir for ir in (woofer_full_ir, tweeter_full_ir) if ir is not None])

    # Primary responses are first-occurrence-derived. Repeats are attached
    # as diagnostic-only `repeat_responses` on the matching primary.
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    branches = [(seg_w, woofer_full_ir)]
    if seg_t is not None and tweeter_full_ir is not None:
        branches.append((seg_t, tweeter_full_ir))
    responses = tuple(
        replace(
            resp,
            repeat_responses=_repeat_driver_responses(
                program, capture, sample_rate, global_offset, epsilon,
                occurrences_by_role.get(resp.role, ()),
                role=resp.role,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                priors=priors,
            ),
        )
        for resp in (
            _driver_response(
                seg.role, full_ir, sample_rate,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                radiated_band_hz=_radiated_band_hz(seg),
                capture_segment=_raw_sweep_segment(
                    capture, seg, global_offset + seg.start_sample,
                ),
            )
            for seg, full_ir in branches
        )
    )

    # ONE branch: nothing to align across, so both come back absent WITH A REASON.
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    pair_not_evaluated: str | None = MEASURE_PAIR_SINGLE_DRIVER
    if seg_t is not None and tweeter_full_ir is not None and fc_hz is not None:
        pair_not_evaluated = None
        alignment = _estimate_alignment(
            capture, program, sample_rate, global_offset, drift.epsilon_ppm / 1e6,
            fc_hz, geometry, priors,
            woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
            pre_samples=pre_samples,
        )

        # Reads BOTH branches' ALIGNMENT-class verdict (the 35 dB law), not
        # the magnitude one every surface displays.
        alignment_roles = {seg_w.role, seg_t.role}
        branch_snr_insufficient = any(
            driver_alignment_snr_verdict(resp) == ALIGNMENT_SNR_REFUSAL_VERDICT
            for resp in responses
            if resp.role in alignment_roles
        )
        candidate, predicted_sum = _build_candidate(
            woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
            seg_w.role, seg_t.role, alignment, calibration,
            tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
            woofer_sweep_lo_hz=seg_w.f1_hz, tweeter_sweep_hi_hz=seg_t.f2_hz,
            alignment_delay_bounds_us=priors.alignment_delay_bounds_us,
            branch_snr_insufficient=branch_snr_insufficient,
            applied_alignment=priors.applied_alignment,
            explicit_alignment_delay_us=priors.explicit_alignment_delay_us,
            explicit_alignment_polarity_sign=priors.explicit_alignment_polarity_sign,
        )
        # `_build_candidate` owns the selection; the estimate published here
        # must carry what was committed, with correlation's own answer
        # preserved beside it as the seed.
        if candidate.alignment_objective in _SELECTOR_COMMITTED_OBJECTIVES:
            alignment = replace(
                alignment,
                polarity=candidate.polarity,
                polarity_sign=polarity_sign_of(candidate.polarity),
                # READ, not re-derived — the candidate carries the answer.
                polarity_agrees_with_sum=candidate.polarity_agrees_with_sum,
            )
        # The delay half keeps its own condition: the anchor path is where
        # the committed delay can differ from the estimate's GCC seed.
        if candidate.anchor_delay_us is not None:
            alignment = replace(
                alignment,
                delay_us=candidate.delay_us,
                raw_delay_us=candidate.delay_us + alignment.parallax_us,
                seed_delay_us=alignment.delay_us,
                confidence_source="gcc_phat_seed",
            )
    else:
        # One branch radiates the whole band, so the model IS that branch.
        predicted_sum = (responses[0].freqs_hz, responses[0].magnitude_db)
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        drift=drift,
        driver_responses=responses,
        alignment=alignment,
        candidate=candidate,
        measure_pair_not_evaluated=pair_not_evaluated,
        mic_tier=priors.mic_tier,
        mic_calibrated=priors.mic_calibrated,
        # Exact: composition returns its input untouched iff every prior map is None.
        configured_path_composed=(
            priors.configured_crossover_response_by_role is not None
        ),
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=predicted_sum,
        glitch_detected=drift.glitch_detected,
    )


def _build_candidate(
    woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
    woofer_role, tweeter_role, alignment, calibration,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
    woofer_sweep_lo_hz: float | None = None,
    tweeter_sweep_hi_hz: float | None = None,
    alignment_delay_bounds_us: tuple[float, float] | None = None,
    branch_snr_insufficient: bool = False,
    applied_alignment: AppliedAlignment | None = None,
    explicit_alignment_delay_us: float | None = None,
    explicit_alignment_polarity_sign: int | None = None,
) -> tuple[CrossoverCandidate, tuple[np.ndarray, np.ndarray]]:
    freqs, W, gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=calibration)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=calibration)
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=tweeter_sweep_lo_hz, woofer_sweep_hi_hz=woofer_sweep_hi_hz,
    )
    # Gating-consistent prediction: a branch near a reflective mic position
    # can be valid only above a floor HIGHER than the nominal Fc+/-1-oct
    # band. Clamp every W/T-derived quantity to the worse of the two
    # branches' floors; if the floor consumes the whole band,
    # solve_branch_trims/_ripple_db raise into the internal_error seam.
    branch_floor_hz = max(
        (f for f in (_gate_floor_hz(gate_w), _gate_floor_hz(gate_t)) if f is not None),
        default=None,
    )
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )
    # The LEVEL MATCH reads a different span: each branch on its own side of
    # Fc, its own excited-and-gated span (see solve_branch_trims), floored
    # by the shared reflection floor. Missing sweep bound falls back to the
    # nominal Fc-octave edge.
    def _span(lo_hz: float | None, hi_hz: float | None) -> tuple[float, float]:
        lo = float(lo_hz) if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
        hi = float(hi_hz) if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
            lo = max(lo, branch_floor_hz)
        return lo, hi

    woofer_span = _span(woofer_sweep_lo_hz, woofer_sweep_hi_hz)
    tweeter_span = _span(tweeter_sweep_lo_hz, tweeter_sweep_hi_hz)
    trim_w, trim_t_band_average, level_w, level_t = solve_branch_trims(
        freqs, W, T, fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    (w_band_lo, w_band_hi), (t_band_lo, t_band_hi) = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    # The level match's own inputs. A large disagreement with the per-role
    # target_level_db in crossover_v2_linearization_giveback is the
    # signature of a level-frame defect.
    log_event(
        logger, "program_analysis.branch_level_match",
        woofer_role=woofer_role, tweeter_role=tweeter_role,
        fc_hz=round(float(fc_hz), 3),
        level_w_db=round(float(level_w), 3), level_t_db=round(float(level_t), 3),
        woofer_band_hz=(round(w_band_lo, 1), round(w_band_hi, 1)),
        tweeter_band_hz=(round(t_band_lo, 1), round(t_band_hi, 1)),
        trim_band_average_db=round(float(trim_t_band_average), 3),
    )
    # --- the (polarity, delay) pair, on one objective ---
    # Runs BEFORE the trim polish below (which takes the polarity as input):
    # scoring at the polished trim would be circular.
    seed_delay_us = alignment.delay_us
    anchor_delay_us = None
    snap_found = False
    # THE FRAME, gated on the aligner's own status alone. The aligner
    # single-sources the physical peak-gap anchor; everything downstream
    # derives from it rather than re-running the argmax. NOT also gated on
    # declared bounds, which would conflate the frame with the seed
    # question below — measured at a 20.37 dB penalty when confused.
    if alignment.status == ALIGNMENT_OK and alignment.anchor_delay_us is not None:
        anchor_delay_us = float(alignment.anchor_delay_us)
    # THE SEED DELAY — a different question, bounds-gated. The gated
    # local-peak snap is correlation's own refined delay within
    # +/-(period/6) at Fc; with no declared bounds the seed is the bare
    # GCC estimate instead.
    if anchor_delay_us is not None and alignment_delay_bounds_us is not None:
        if alignment.snapped_delay_us is not None:
            seed_delay_us = float(alignment.snapped_delay_us)
            snap_found = True
        else:
            seed_delay_us = anchor_delay_us
    # A refused estimate is not a seed to search around: nothing downstream
    # applies its delay or polarity, so scoring one would grade a pair that
    # can never ship.
    selection = (
        _select_alignment_pair(
            freqs, W, T,
            fc_hz=fc_hz, lo_hz=lo_clamped, hi_hz=hi,
            trim_w_db=trim_w, trim_t_db=trim_t_band_average,
            anchor_delay_us=anchor_delay_us,
            seed_delay_us=seed_delay_us,
            seed_polarity_sign=alignment.polarity_sign,
            delay_bounds_us=alignment_delay_bounds_us,
            branch_snr_insufficient=branch_snr_insufficient,
            applied_alignment=applied_alignment,
            explicit_delay_us=explicit_alignment_delay_us,
            explicit_polarity_sign=explicit_alignment_polarity_sign,
        )
        if alignment.status == ALIGNMENT_OK
        else None
    )
    if selection is None:
        polarity_sign = alignment.polarity_sign
        delay_us = seed_delay_us
        seed_ripple_db = None
        flatness_improvement_db = None
        left_anchor_lobe = False
        alignment_objective = (
            ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED
            if alignment.status != ALIGNMENT_OK
            else ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND
        )
    else:
        polarity_sign = selection.polarity_sign
        delay_us = selection.delay_us
        alignment_objective = selection.objective
        left_anchor_lobe = selection.left_anchor_lobe
        seed_ripple_db = flatness_improvement_db = None
        if math.isfinite(selection.seed_ripple_db) and math.isfinite(selection.ripple_db):
            seed_ripple_db = selection.seed_ripple_db
            flatness_improvement_db = selection.flatness_improvement_db
        log_event(
            logger, "program_analysis.alignment_selection",
            # WARNING on: correlation losing the polarity, any non-flat-sum
            # commitment, or leaving the anchor's comb lobe.
            level=(
                logging.INFO if (
                    selection.objective == ALIGNMENT_COMMITTED_FLAT_SUM
                    and selection.polarity_agrees_with_sum
                    and not selection.left_anchor_lobe
                ) else logging.WARNING
            ),
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            objective=selection.objective,
            fc_hz=round(float(fc_hz), 3),
            band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
            polarity=polarity_label(selection.polarity_sign),
            delay_us=round(float(selection.delay_us), 3),
            # BOTH ripples go through the None-safe rounder: the low-SNR
            # path commits without a search, so a NaN branch can reach either.
            ripple_db=_finite_or_none(selection.ripple_db, 4),
            seed_polarity=polarity_label(selection.seed_polarity_sign),
            seed_delay_us=round(float(selection.seed_delay_us), 3),
            seed_ripple_db=_finite_or_none(selection.seed_ripple_db, 4),
            polarity_agrees_with_sum=selection.polarity_agrees_with_sum,
            flatness_improvement_db=_finite_or_none(
                selection.flatness_improvement_db, 4,
            ),
            grid_points=selection.grid_points,
            grid_step_us=round(float(selection.grid_step_us), 3),
            branch_snr_insufficient=bool(branch_snr_insufficient),
            # The number that WAS held (or None), beside the anchor that was
            # DECLINED, separating "design asks for none" from "could not read".
            applied_delay_us=(
                None if applied_alignment is None or applied_alignment.delay_us is None
                else round(float(applied_alignment.delay_us), 3)
            ),
            applied_alignment_present=applied_alignment is not None,
            # None on every ordinary session; non-None is greppable as "this
            # round's delay was prescribed, not searched".
            prescribed_delay_us=(
                None if explicit_alignment_delay_us is None
                else round(float(explicit_alignment_delay_us), 3)
            ),
            # None when no prescription was made; "unpinned" when one was
            # made and left the basin to the objective.
            prescribed_polarity=(
                None if explicit_alignment_delay_us is None
                else "unpinned" if explicit_alignment_polarity_sign is None
                else polarity_label(int(explicit_alignment_polarity_sign))
            ),
            anchor_delay_us=(
                None if anchor_delay_us is None
                else round(float(anchor_delay_us), 3)
            ),
            left_anchor_lobe=selection.left_anchor_lobe,
        )
    # A prescription that never reached a commitment must not be absorbed
    # quietly: emitted AFTER the block above so it can name what WAS
    # committed instead of printing null.
    if (
        explicit_alignment_delay_us is not None
        and alignment_objective not in ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES
    ):
        log_event(
            logger, "program_analysis.alignment_prescription_not_committed",
            level=logging.WARNING,
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            fc_hz=round(float(fc_hz), 3),
            prescribed_delay_us=round(float(explicit_alignment_delay_us), 3),
            committed_delay_us=round(float(delay_us), 3),
            prescribed_polarity=(
                "unpinned" if explicit_alignment_polarity_sign is None
                else polarity_label(int(explicit_alignment_polarity_sign))
            ),
            committed_polarity=polarity_label(polarity_sign),
            alignment_status=alignment.status,
            objective=alignment_objective,
        )
    snap_delta_us = None if anchor_delay_us is None else delay_us - anchor_delay_us
    # Polish the tweeter trim for minimum summed-response ripple, seeded by
    # the band-average match, guarded against a result further than
    # REALIZED_LEVEL_MATCH_TOLERANCE_DB from the seed.
    #
    # Run ONLY where summed ripple can express a level: on tweeter-sweep-
    # starts-at-Fc geometry the shared band is one-sided (woofer 20+ dB
    # down its skirt), so the objective moved a trim 7.9 dB on one archived
    # capture with only the sanity guard stopping it — a selector that
    # cannot see the woofer must not set its handoff level.
    ripple_band_straddles_fc = lo_clamped < fc_hz < hi
    ripple_polish_rejected_delta_db: float | None = None
    if ripple_band_straddles_fc:
        trim_t_ripple, _ripple_t_ripple, _seed = solve_ripple_optimal_trim(
            freqs, W, T, fc_hz,
            lo_hz=lo_clamped, hi_hz=hi,
            seed_trim_db=trim_t_band_average,
            trim_w_db=trim_w,
            sign=polarity_sign,
        )
        # Admitted only where the pair it produces can be GRADED as level matched.
        polish_delta_db = trim_t_ripple - trim_t_band_average
        if abs(polish_delta_db) > REALIZED_LEVEL_MATCH_TOLERANCE_DB:
            ripple_polish_rejected_delta_db = float(polish_delta_db)
            log_event(
                logger, "program_analysis.ripple_trim_rejected",
                level=logging.WARNING,
                woofer_role=woofer_role, tweeter_role=tweeter_role,
                band_average_trim_db=round(trim_t_band_average, 3),
                ripple_optimal_trim_db=round(trim_t_ripple, 3),
                rejected_delta_db=round(polish_delta_db, 3),
                tolerance_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB,
            )
            trim_t = trim_t_band_average
        else:
            trim_t = trim_t_ripple
    else:
        log_event(
            logger, "program_analysis.ripple_trim_skipped",
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            reason="ripple_band_one_sided",
            fc_hz=round(float(fc_hz), 3),
            ripple_band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
            band_average_trim_db=round(trim_t_band_average, 3),
        )
        trim_t = trim_t_band_average
    # TWO sums, two questions, two owners.
    #
    # `predicted_aligned` — the flattest-achievable, INDEPENDENTLY ALIGNED sum.
    # It answers "how coherently can this capture's two branches sum at all?",
    # a property of the measurement and not of the delay selection. It is the
    # ONLY input to `predicted_ripple_db`, and therefore to `crossover_v2_flow`'s
    # `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` threshold, which that constant
    # documents as calibrated against a fixed hardware corpus scored on THIS
    # metric — the zero-residual ripple, not the delay-carrying one. Crossing it
    # DISCLOSES rather than refuses.
    #
    # Why the disclosure keeps this frame: a candidate's own committed delay can
    # LOWER its ripple, so pointing it at a delay-carrying curve would let a
    # capture whose branches sum incoherently slip under the threshold on its
    # own alignment — and a reservation a capture can talk itself out of is
    # exactly as dishonest as a veto it could. Measured on a banked JTS3 capture
    # (`captures/r10b-alignment-20260801/ripple_vs_residual_sweep.py`): sweeping
    # the residual across the ±(period/6) snap radius, 32 of 84 sampled
    # residuals come in BELOW the zero-residual 14.8831 dB, bottoming at
    # 14.0744 dB — and that capture sits 0.12 dB under the 15.0 dB ceiling, so
    # the 0.81 dB an alignment could buy is not a hypothetical margin.
    predicted_aligned = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
    )
    ripple = _ripple_db(freqs, predicted_aligned, lo_clamped, hi)
    # `predicted_applied` — the same two branches under the delay this candidate
    # actually COMMITS. This is what gets persisted as
    # `ProgramAnalysis.predicted_sum` and becomes VERIFY's tracking reference,
    # so that comparison grades measured-vs-the-applied-model (model fidelity)
    # rather than measured against a target no realizable delay produces.
    #
    # The RESIDUAL relative to the argmax-referenced frame, never the
    # applied delay itself (see `summed_model_residual_delay_us`'s
    # double-count hazard). Gated on the aligner's own status rather than
    # the snap block's condition, so a hand-built refused estimate is never
    # modelled as though a delay ran. The SNR refusal also withdraws the
    # anchor: on that path `committed - anchor` measures disagreement
    # between an untrusted anchor and a trusted applied delay, not the
    # speaker, and this model is graded by VERIFY's tracking reference —
    # phasing it would let an untrusted number kill a correctly-aligned
    # speaker there. `snap_delta_us` still RECORDS the disagreement.
    _alignment_unmeasured = (
        alignment_objective in ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    )
    residual_delay_us = summed_model_residual_delay_us(
        alignment.anchor_delay_us
        if alignment.status == ALIGNMENT_OK and not _alignment_unmeasured
        else None,
        delay_us,
    )
    predicted_applied = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
        freqs_hz=freqs,
        residual_delay_us=residual_delay_us,
    )
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted_applied), 1e-12))
    candidate = CrossoverCandidate(
        trim_db={woofer_role: trim_w, tweeter_role: trim_t},
        polarity=polarity_label(polarity_sign),
        delay_us=delay_us,
        predicted_ripple_db=ripple,
        confidence=alignment.confidence,
        alignment_seed_ripple_db=seed_ripple_db,
        flatness_improvement_db=flatness_improvement_db,
        anchor_delay_us=anchor_delay_us,
        snap_delta_us=snap_delta_us,
        snap_found=snap_found,
        trim_band_average_db={woofer_role: trim_w, tweeter_role: trim_t_band_average},
        alignment_objective=alignment_objective,
        seed_polarity_sign=alignment.polarity_sign,
        left_anchor_lobe=left_anchor_lobe,
        # From the selection that MADE the comparison, never re-derived.
        polarity_agrees_with_sum=(
            None if selection is None else selection.polarity_agrees_with_sum
        ),
        polarity_pinned=bool(selection is not None and selection.polarity_pinned),
        ripple_polish_rejected_delta_db=ripple_polish_rejected_delta_db,
    )
    return candidate, (freqs, predicted_db)


#: ``verify_absolute["not_evaluated"]`` reasons: "nobody graded this" must
#: never render as "this passed".
ABSOLUTE_NO_FC = "no_crossover_fc"
ABSOLUTE_NO_TARGET = "no_candidate_crossover_target"
ABSOLUTE_NO_TRUSTED_BAND = "no_trusted_crossover_region"
#: The speaker HAS no crossover region — a 1-way main (#3480), distinct from
#: the three above, which say a round could not establish a region it does have.
ABSOLUTE_NO_CROSSOVER_TOPOLOGY = "no_crossover_topology"


def _verify_absolute_result(
    summed, segment, fc_hz, priors, measured_db=None,
) -> dict[str, Any]:
    """Measured summed response vs the CANDIDATE'S OWN crossover target across
    the crossover region — the absolute half of "did this crossover hand off
    as designed?".

    The target is ``20log10|sum_role sign_role*C_role(f)|``: the coherent
    sum of the committed crossover transfers. ``priors.predicted_sum`` is
    built from the MEASURED branches (null included); this curve is built
    from the crossover alone and says what the candidate is SUPPOSED to sum
    to. No level or trim enters it — the grader below is offset-invariant.

    Numbers only; tolerance and verdict belong to ``crossover_v2_flow``.
    ``worst_db`` is SIGNED with its frequency, since a dip and a peak are
    opposite defects. Measured is smoothed, the target is not (an analytic
    response with no noise to smooth).
    """
    transfers = priors.configured_crossover_response_by_role
    if transfers is not None and not transfers:
        # An EMPTY map, never a missing one — "has no crossover" vs "nobody
        # said where", both arriving with fc_hz is None.
        return {"not_evaluated": ABSOLUTE_NO_CROSSOVER_TOPOLOGY}
    if fc_hz is None:
        return {"not_evaluated": ABSOLUTE_NO_FC}
    if not transfers:
        return {"not_evaluated": ABSOLUTE_NO_TARGET}
    band = crossover_region_band_hz(
        fc_hz,
        validity_floor_hz=summed.validity_floor_hz,
        radiated_band_hz=_radiated_band_hz(segment),
    )
    if band is None:
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    mask = (summed.freqs_hz >= band[0]) & (summed.freqs_hz <= band[1])
    if not np.any(mask):
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    freqs = np.asarray(summed.freqs_hz, dtype=np.float64)
    total = np.zeros(freqs.shape, dtype=np.complex128)
    signs = priors.configured_polarity_sign_by_role or {}
    for role, response in transfers.items():
        sign = -1 if int(signs.get(role, 1)) < 0 else 1
        total = total + sign * np.asarray(response(freqs), dtype=np.complex128)
    target_db = 20.0 * np.log10(np.maximum(np.abs(total), 1e-12))
    if measured_db is None:
        measured_db = analysis_mod.smooth_fractional_octave(
            freqs, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
    # The SAME offset-invariant grader the tracking pair uses, so two numbers
    # on one screen mean the same kind of thing.
    rms_db, max_db = analysis_mod.tracking_error_db(
        freqs, measured_db, target_db, band,
    )
    deviation = measured_db[mask] - target_db[mask]
    deviation = deviation - float(np.mean(deviation))
    worst = int(np.argmax(np.abs(deviation)))
    return {
        "band_hz": [float(band[0]), float(band[1])],
        "rms_db": float(rms_db),
        "max_db": float(max_db),
        "worst_db": float(deviation[worst]),
        "worst_hz": float(freqs[mask][worst]),
        "n_bins": int(np.count_nonzero(mask)),
    }


def _analyze_verify(
    program, capture, sample_rate, global_offset, locations,
    calibration, priors, frame_ledger,
) -> ProgramAnalysis:
    fc_hz = float(priors.crossover_fc_hz) if priors.crossover_fc_hz else None
    seg = program.segment("sweep_verify")
    full_ir, _pre = _deconvolve_window(
        capture, seg, global_offset + seg.start_sample, sample_rate
    )
    n_fft = _n_fft_for(full_ir)
    summed = _driver_response(
        "summed", full_ir, sample_rate,
        calibration=calibration, ambient_report=None, fc_hz=fc_hz, n_fft=n_fft,
        radiated_band_hz=_radiated_band_hz(seg),
    )
    # The tracking comparator below is deliberately NOT re-based onto the
    # spatial cloud's shared spec curve: "did apply do what the model
    # predicted" is a single-position claim (both sides share that
    # geometry), while "is the speaker flat" is graded on the cloud.
    # Feeding the cloud's spatial average in here would read spatial
    # variation as a false tracking failure.
    ripple = None
    reverse_null_depth = None
    tracking = None
    tracking_curve = None
    measured_db = None
    band = verify_tracking_band_hz(
        fc_hz,
        radiated_band_hz=_radiated_band_hz(seg),
        measure_excited_band_hz=priors.measure_excited_band_hz,
    )
    # The ripple and the reverse null are statements about a handoff, so they
    # stay behind the corner rather than behind the band.
    if fc_hz is not None and band is not None:
        lo, hi = band
        ripple = _ripple_db(summed.freqs_hz, summed.complex_tf, lo, hi)
        # Guarded: depth is read at Fc/2 and 2*Fc, np.interp CLAMPS outside
        # the data, so a curve not reaching both shoulders (or below the
        # gate's validity floor) would return an edge-value number with
        # nothing saying so. No number is the honest answer there.
        from jasper.audio_measurement.analysis import crossover_null_depth_db

        lower_shoulder_hz = fc_hz / 2.0
        floor_hz = summed.validity_floor_hz
        spans_shoulders = (
            float(summed.freqs_hz[0]) <= lower_shoulder_hz
            and float(summed.freqs_hz[-1]) >= fc_hz * 2.0
        )
        above_floor = floor_hz is None or lower_shoulder_hz >= floor_hz
        if spans_shoulders and above_floor:
            reverse_null_depth = crossover_null_depth_db(
                summed.freqs_hz, summed.magnitude_db, fc_hz,
            )
    if band is not None and priors.predicted_sum is not None:
        lo, hi = band
        pred_freqs, pred_db = priors.predicted_sum
        measured_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION
        )
        predicted_db_interp = np.interp(summed.freqs_hz, pred_freqs, pred_db)
        predicted_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz,
            predicted_db_interp,
            VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
        # Validity-floor clamp: this capture's own reflection gate can be
        # tighter than the band above at a reflective mic position — bins
        # below it are a truncated-gate artifact, not a measurement, and
        # must not decide PASS/FAIL. Composes with notch exclusion (clamp
        # first, then still exclude a genuine deep predicted notch above it).
        floor_hz = summed.validity_floor_hz
        lo_clamped = (
            max(lo, floor_hz) if floor_hz is not None and math.isfinite(floor_hz) else lo
        )
        tracking_band = (lo_clamped, hi)
        rms, max_abs = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
        )
        # Notch-excluded: the actual gating comparator reads max_db_notch_excluded.
        rms_excl, max_excl = analysis_mod.notch_excluded_tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        # Raw full-band (pre-floor-clamp), DIAGNOSTIC only, never gated on.
        raw_rms, raw_max = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, (lo, hi),
        )
        # Hand the delta probe the same curves these scalars were reduced from.
        tracking_curve = (summed.freqs_hz, measured_db, predicted_db)
        tracking = {
            "rms_db": rms,
            "max_db": max_abs,
            "rms_db_notch_excluded": rms_excl,
            "max_db_notch_excluded": max_excl,
            "tracking_band_hz": [tracking_band[0], tracking_band[1]],
            "rms_db_full_band": raw_rms,
            "max_db_full_band": raw_max,
        }
        # FRAME DISCIPLINE. predicted_db (on-axis, MEASURE sitting) and
        # measured_db (in-room, VERIFY sitting) are not in the same frame —
        # on one corpus a -0.79 dB/octave tilt between them accounted for
        # 84% of a "predictions are optimistic" headline. So the frame is
        # fitted and the residual reported BOTH ways; max_db_notch_excluded
        # still gates, since a measured tilt is EVIDENCE, not permission to
        # re-grade without attribution.
        #
        # FITTED OVER TRUSTED BINS ONLY (validity-floor band minus
        # deep-predicted-notch bins): inside a modelled notch the depth is
        # hypersensitive, and a straight line through one lets the notch
        # lever the slope (measured: a 25 dB edge notch flips an injected
        # -0.800 dB/octave frame to +0.226). This reduces the lever without
        # removing it — a wide surviving skirt still biases the estimate,
        # so ``tilt_removed <= raw`` is not a theorem.
        frame_mask = analysis_mod.notch_excluded_band_mask(
            summed.freqs_hz, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        frame = fit_frame(
            summed.freqs_hz[frame_mask],
            measured_db[frame_mask],
            predicted_db[frame_mask],
        )
        tilt_removed_rms_db: float | None = None
        tilt_removed_max_db: float | None = None
        if frame.fitted:
            # One frame, removed from the measured curve, then each grade
            # re-taken by its own grader. Both graders mean-centre their
            # error, so the frame's OFFSET term cannot move either number;
            # only the TILT can — hence "tilt_removed".
            deframed_db = measured_db - frame.frame_db(summed.freqs_hz)
            tilt_removed_rms_db = analysis_mod.tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
            )[0]
            tilt_removed_max_db = analysis_mod.notch_excluded_tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
                notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
                notch_reference_db=predicted_db_interp,
            )[1]
        # raw_max_db is the NOTCH-EXCLUDED max (what the tolerance gates
        # on), not max_abs — pairing a tilt-removed excluded max against a
        # raw non-excluded one would be two bin sets under one label.
        tracking["frame"] = FrameComparison(
            fit=frame,
            raw_rms_db=rms,
            raw_max_db=max_excl,
            tilt_removed_rms_db=tilt_removed_rms_db,
            tilt_removed_max_db=tilt_removed_max_db,
        ).to_dict()
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    # Computed on EVERY verify-shaped analysis: the tracking comparison
    # above is exactly what a spliced/clipped recording invalidates.
    integrity = _verify_capture_integrity(
        program, sample_rate, locations, frame_ledger,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        summed_response=summed,
        summed_ripple_db=ripple,
        reverse_null_depth_db=reverse_null_depth,
        verify_tracking=tracking,
        verify_absolute=_verify_absolute_result(
            summed, seg, fc_hz, priors, measured_db=measured_db,
        ),
        verify_tracking_curve=tracking_curve,
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        capture_integrity=integrity,
        glitch_detected=integrity.glitched,
    )


# --------------------------------------------------------------------------- #
# diagnostic summary (operator capture retention — jasper.web.correction_crossover_v2)
# --------------------------------------------------------------------------- #


def _gate_window_ms_of(response: "DriverResponse | None") -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def driver_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant MAGNITUDE-class capture-SNR verdict
    (``ok``/``reduced``/``insufficient``/``unknown``). ``None`` when no
    verdict was computed (a CHECK with no ambient window). The alignment
    refusal reads the OTHER verdict — see :func:`driver_alignment_snr_verdict`.
    """
    return _snr_verdict_of(response.snr if response is not None else None)


def driver_alignment_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant ALIGNMENT-class capture-SNR verdict: 35 dB
    (``DRIVER.alignment_snr_ok_db``), ok-or-insufficient with no reduced
    rung. ``None`` when no verdict was computed — never guessed, so a
    capture with no ambient window keeps the flat-sum selector.
    """
    snr = response.snr if response is not None else None
    if not snr:
        return None
    return _snr_verdict_of(snr.get(DRIVER_SNR_ALIGNMENT_KEY))


def _snr_verdict_of(block: Mapping[str, Any] | None) -> str | None:
    """``block["worst_relevant"]["verdict"]`` when it is a string, else None."""
    if not block:
        return None
    worst = block.get("worst_relevant") or {}
    verdict = worst.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _gate_floor_source_of(response: "DriverResponse | None") -> str | None:
    """WHY this capture's gate window is what it is.

    ``window_ms`` alone can't distinguish the two states, which print
    identically: ``FLOOR_MEASURED`` (a reflection onset was found) vs
    ``FLOOR_SEARCH_BOUND`` (the search ran to ``SEARCH_T_MAX_MS`` without
    finding one and was CAPPED). ``None`` for an ungateable capture.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_disclosure_of(response: "DriverResponse | None") -> str | None:
    """The gate's provenance as a SENTENCE, for the retained sidecar
    (rendered by the one writer, :func:`gate_disclosure.describe_gate`).
    """
    if response is None or not response.gating:
        return None
    return gate_disclosure.describe_gate(response.gating)


def analysis_diagnostic_summary(analysis: Any) -> dict[str, Any]:
    """Flat, JSON-safe numeric diagnostics from one :class:`ProgramAnalysis`,
    compared against a banked corpus by the distortion replays. Reads only
    existing fields, nothing recomputed. Deliberately duck-typed and
    defensive (``getattr(..., None)`` throughout) so a malformed or foreign
    ``analysis`` degrades to an emptier summary rather than raising.
    """
    out: dict[str, Any] = {"phase": getattr(analysis, "phase", None)}

    drift = getattr(analysis, "drift", None)
    if drift is not None:
        out["epsilon_ppm"] = round(float(drift.epsilon_ppm), 3)
        out["max_residual_samples"] = round(float(drift.max_residual_samples), 3)
        out["repeat_level_delta_db"] = round(
            float(getattr(drift, "repeat_level_delta_db", 0.0)), 3
        )
        out["glitch_detected"] = bool(drift.glitch_detected)
        out["glitch_inputs"] = ",".join(getattr(drift, "glitch_inputs", ()) or ())
        # May be DISCONTINUITY_UNRESOLVED (a str); float() would raise.
        discontinuity = getattr(drift, "discontinuity_samples", 0.0)
        out["discontinuity_samples"] = (
            round(float(discontinuity), 3)
            if isinstance(discontinuity, (int, float))
            else discontinuity
        )
        out["discontinuity_after_segment"] = getattr(
            drift, "discontinuity_after_segment", "",
        )
        # Diagnostic-only, never gated.
        for role, eps in (getattr(drift, "per_role_epsilon_ppm", None) or {}).items():
            out[f"{role}_repeat_epsilon_ppm"] = round(float(eps), 3)

    alignment = getattr(analysis, "alignment", None)
    if alignment is not None:
        out["alignment_confidence"] = round(float(alignment.confidence), 4)
        out["alignment_confidence_source"] = getattr(
            alignment, "confidence_source", "gcc_phat",
        )
        out["alignment_status"] = alignment.status
        out["delay_us"] = round(float(alignment.delay_us), 3)
        seed_delay_us = getattr(alignment, "seed_delay_us", None)
        if seed_delay_us is not None:
            out["alignment_seed_delay_us"] = round(float(seed_delay_us), 3)
            out["alignment_refinement_delta_us"] = round(
                float(alignment.delay_us) - float(seed_delay_us), 3,
            )
        out["polarity"] = alignment.polarity
        # The polarity half of the same seed-vs-committed pair the two lines
        # above report for the delay. ``None`` on an estimate nothing
        # cross-checked; the sidecar carries the tri-state rather than
        # flattening "correlation agreed" and "nobody asked" together.
        out["polarity_agrees_with_sum"] = getattr(
            alignment, "polarity_agrees_with_sum", None,
        )

    candidate = getattr(analysis, "candidate", None)
    if candidate is not None:
        out["predicted_ripple_db"] = round(float(candidate.predicted_ripple_db), 4)
        out["alignment_objective"] = getattr(candidate, "alignment_objective", "")
        seed_polarity_sign = getattr(candidate, "seed_polarity_sign", None)
        out["seed_polarity"] = (
            None if seed_polarity_sign is None
            else polarity_label(int(seed_polarity_sign))
        )
        out["left_anchor_lobe"] = bool(getattr(candidate, "left_anchor_lobe", False))
        rejected_polish_db = getattr(
            candidate, "ripple_polish_rejected_delta_db", None
        )
        out["ripple_polish_rejected_delta_db"] = (
            None if rejected_polish_db is None else round(float(rejected_polish_db), 3)
        )
        anchor_delay_us = getattr(candidate, "anchor_delay_us", None)
        if anchor_delay_us is not None:
            out["anchor_delay_us"] = round(float(anchor_delay_us), 3)
            snap_delta_us = getattr(candidate, "snap_delta_us", None)
            out["snap_delta_us"] = (
                round(float(snap_delta_us), 3) if snap_delta_us is not None else None
            )
            out["snap_found"] = bool(getattr(candidate, "snap_found", False))
        if candidate.alignment_seed_ripple_db is not None:
            out["alignment_seed_ripple_db"] = round(
                float(candidate.alignment_seed_ripple_db), 4,
            )
            out["flatness_improvement_db"] = round(
                float(candidate.flatness_improvement_db), 4,
            )

    for resp in getattr(analysis, "driver_responses", None) or ():
        role = resp.role
        out[f"{role}_gate_window_ms"] = _gate_window_ms_of(resp)
        out[f"{role}_gate_floor_source"] = _gate_floor_source_of(resp)
        out[f"{role}_gate_disclosure"] = _gate_disclosure_of(resp)
        out[f"{role}_validity_floor_hz"] = resp.validity_floor_hz
        if resp.snr is not None:
            worst = resp.snr.get("worst_relevant") or {}
            out[f"{role}_snr_db"] = worst.get("estimated_snr_db")
            out[f"{role}_snr_verdict"] = driver_snr_verdict(resp)
            out[f"{role}_snr_band"] = worst.get("band_id")
            # The ALIGNMENT-class trio, beside the MAGNITUDE one — a
            # separate law (35 dB, no reduced rung) from magnitude's 25 dB.
            alignment = resp.snr.get(DRIVER_SNR_ALIGNMENT_KEY) or {}
            alignment_worst = alignment.get("worst_relevant") or {}
            out[f"{role}_alignment_snr_db"] = alignment_worst.get(
                "estimated_snr_db"
            )
            out[f"{role}_alignment_snr_verdict"] = driver_alignment_snr_verdict(
                resp
            )
            out[f"{role}_alignment_snr_band"] = alignment_worst.get("band_id")

    for pilot in getattr(analysis, "pilots", None) or ():
        role = pilot.role
        snr_db = getattr(pilot, "snr_db", math.inf)
        out[f"{role}_pilot_snr_db"] = round(snr_db, 2) if math.isfinite(snr_db) else None
        out[f"{role}_captured_delta_db"] = round(float(pilot.captured_delta_db), 3)
        out[f"{role}_programmed_delta_db"] = round(float(pilot.programmed_delta_db), 3)
        out[f"{role}_channel_map_target_rise_db"] = pilot.channel_map_target_rise_db
        out[f"{role}_channel_map_cross_rise_db"] = pilot.channel_map_cross_rise_db
        # Both raws AND the ratio: raws say which half moved, ratio decided CROSS.
        out[f"{role}_channel_map_isolation_db"] = channel_map_isolation_db(
            pilot.channel_map_target_rise_db, pilot.channel_map_cross_rise_db
        )

    gain_plan = getattr(analysis, "gain_plan", None)
    if gain_plan is not None:
        out["gain_plan_snr_floor_ok"] = gain_plan.snr_floor_ok
        out["gain_plan_predicted_peak_dbfs"] = round(
            float(gain_plan.predicted_peak_dbfs), 3
        )
        for role, solve in (getattr(gain_plan, "role_solves", None) or {}).items():
            out[f"{role}_measure_gain_db"] = round(float(solve.gain_db), 3)
            out[f"{role}_measure_gain_reduction_db"] = round(
                float(solve.reduction_db), 3
            )
            out[f"{role}_measure_gain_bound_by"] = solve.bound_by

    for flag in ("pilot_snr_ok", "linearity_ok", "channel_map_ok"):
        value = getattr(analysis, flag, None)
        if value is not None:
            out[flag] = value

    # Absent, not empty, on CHECK/MEASURE (whose glitch verdict is `drift` above).
    integrity = getattr(analysis, "capture_integrity", None)
    if integrity is not None:
        out["integrity_failed"] = ",".join(getattr(integrity, "failed", ()) or ())
        out["integrity_not_evaluated"] = ",".join(
            getattr(integrity, "not_evaluated", ()) or ()
        )
        out["integrity_locate_confidence_min"] = getattr(
            integrity, "locate_confidence_min", None
        )
        out["integrity_schedule_residual_ms_worst"] = getattr(
            integrity, "schedule_residual_ms_worst", None
        )
        out["integrity_clipped_segments"] = ",".join(
            getattr(integrity, "clipped_segments", ()) or ()
        )

    # Present on EVERY phase, unlike the integrity block above.
    ledger = getattr(analysis, "frame_ledger", None)
    if ledger is not None:
        out["frames_received"] = ledger.received_frames
        out["frames_declared"] = ledger.declared_frames
        out["frames_encoded"] = ledger.encoded_frames
        out["frames_render_gaps"] = ledger.render_gaps
        out["frames_render_gap_frames"] = ledger.render_gap_frames
        out["frames_lost_at"] = ",".join(ledger.lost_at)

    summed_response = getattr(analysis, "summed_response", None)
    if summed_response is not None:
        out["verify_gate_window_ms"] = _gate_window_ms_of(summed_response)
        out["verify_gate_floor_source"] = _gate_floor_source_of(summed_response)
        out["verify_gate_disclosure"] = _gate_disclosure_of(summed_response)
        out["verify_validity_floor_hz"] = summed_response.validity_floor_hz

    tracking = getattr(analysis, "verify_tracking", None)
    if tracking:
        for key in ("rms_db", "max_db", "rms_db_notch_excluded", "max_db_notch_excluded"):
            if key in tracking:
                out[key] = tracking[key]
        band = tracking.get("tracking_band_hz")
        if isinstance(band, (list, tuple)) and len(band) == 2:
            out["tracking_band_lo_hz"] = band[0]
            out["tracking_band_hi_hz"] = band[1]
        # Frame terms ride the sidecar BESIDE the raw scalars, never instead
        # of them; present with None terms when no frame could be fitted.
        frame = tracking.get("frame")
        if isinstance(frame, Mapping):
            out["frame_offset_db"] = frame.get("offset_db")
            out["frame_tilt_db_per_octave"] = frame.get("tilt_db_per_octave")
            out["frame_pivot_hz"] = frame.get("pivot_hz")
            out["frame_n_bins"] = frame.get("n_bins")
            tilt_removed = frame.get("tilt_removed")
            if isinstance(tilt_removed, Mapping):
                out["rms_db_tilt_removed"] = tilt_removed.get("rms_db")
                out["max_db_notch_excluded_tilt_removed"] = tilt_removed.get("max_db")

    return out
